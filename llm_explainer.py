"""
llm_explainer.py  —  SCAI doctor-facing attention-note generator.

Loads LFM2.5-1.2B-Instruct + your fine-tuned LoRA adapter ONCE, and turns the CNN's
attention map into a short Arabic note telling the doctor where the model focused.

It is NOT a diagnosis. It only describes attention regions — the exact scope the model
was trained on.

PLACE THE ADAPTER FILES in:  <project>/model/llm_adapter/
  adapter_config.json        <- REQUIRED (grab it from ./scai-notes-lora)
  adapter_model.safetensors
  tokenizer_config.json
  tokenizer.json
  chat_template.jinja
(training_args.bin is NOT needed for inference.)

The base model downloads from Hugging Face on first run; after that it is cached. For a
fully offline clinic, pre-download it once or merge the adapter (see notes at the bottom).
"""

import os
import numpy as np

# ---- prompt format: MUST match training (scai.py makenotes) exactly ----------------
NOTE_SYSTEM = (
    "أنت مساعد توضيح سريري. مهمتك فقط وصف المناطق الزمنية التي ركّز عليها نموذج تخطيط "
    "القلب (خريطة الانتباه) لتوجيه نظر الطبيب إليها للتدقيق. "
    "يمنع منعاً باتاً ذكر أي تشخيص أو معدل نبض أو حكم على الحالة. صف موضع التركيز فقط بإيجاز."
)

BASE_MODEL = "LiquidAI/LFM2.5-1.2B-Instruct"
ADAPTER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model", "llm_adapter")

# Fixed, SAFE sentence for the zero-zone / noisy case. We NEVER let the model speak here,
# because with no focus region it tends to hallucinate one (we saw this in testing).
_ZERO_ZONE_NOTE = "لم تتركّز خريطة الانتباه على مقطع واضح؛ يُنصح بمراجعة جودة التسجيل بالكامل."

_model = None
_tok = None


def _load():
    """Lazy-load the model+adapter once, on first use."""
    global _model, _tok
    if _model is not None:
        return
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from peft import PeftModel
    # tokenizer comes from the adapter dir (it carries the chat template you trained with)
    _tok = AutoTokenizer.from_pretrained(ADAPTER_DIR, trust_remote_code=True)
    base = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL, torch_dtype=torch.float32, device_map="cpu", trust_remote_code=True
    )
    _model = PeftModel.from_pretrained(base, ADAPTER_DIR)
    _model.eval()


def summarize_xai(xai_weights, fs=300, thresh=0.6, max_locs=6):
    """Same logic as scai.summarize_xai: pattern + zone count + zone time-ranges."""
    w = np.asarray(xai_weights, dtype=np.float64)
    if w.size == 0 or np.all(w == 0):
        return "Corrupted/Noisy", 0, []
    hi = w >= thresh
    zones, in_zone, start = [], False, 0
    for i, v in enumerate(hi):
        if v and not in_zone:
            in_zone, start = True, i
        elif not v and in_zone:
            in_zone = False
            zones.append((round(start / fs, 1), round((i - 1) / fs, 1)))
    if in_zone:
        zones.append((round(start / fs, 1), round((len(hi) - 1) / fs, 1)))
    n = len(zones)
    frac = hi.mean()
    if n == 0:
        pat = "Corrupted/Noisy"
    elif n <= 2 and frac < 0.15:
        pat = "Isolated/Concentrated"
    elif n >= 6 or frac > 0.4:
        pat = "Scattered/Widespread"
    else:
        pat = "Segmented"
    return pat, n, zones[:max_locs]


def explain_attention(xai_weights):
    """Public entry point. Returns a short Arabic attention note for the doctor."""
    pattern, zones, locs = summarize_xai(xai_weights)

    # SAFETY GUARD: no zones -> fixed sentence, never call the model (avoids hallucination)
    if zones == 0:
        return _ZERO_ZONE_NOTE

    loc_txt = "، ".join(f"[{a}s-{b}s]" for a, b in locs) if locs else "—"
    user = f"Pattern: {pattern} | Zones_Count: {zones} | Locations: {loc_txt}"

    try:
        _load()
        import torch
        ids = _tok.apply_chat_template(
            [{"role": "system", "content": NOTE_SYSTEM}, {"role": "user", "content": user}],
            add_generation_prompt=True, return_tensors="pt",
        )
        with torch.no_grad():
            out = _model.generate(ids, max_new_tokens=80, do_sample=False,
                                  pad_token_id=_tok.pad_token_id or _tok.eos_token_id)
        return _tok.decode(out[0][ids.shape[1]:], skip_special_tokens=True).strip()
    except Exception as e:
        print("LLM explainer error:", e)
        # graceful fallback: a plain, honest description built from the zones
        return f"ركّز النموذج على {zones} منطقة ({loc_txt})؛ يُنصح بتدقيق هذه المقاطع."


# ---------------------------------------------------------------------------
# OFFLINE / SPEED notes:
# - First call downloads the base model (~2.3GB) and is slow; later calls are warm.
# - On CPU, generation takes a few seconds — fine for a doctor-facing note, not real-time.
# - For a faster, dependency-light deployment, merge the adapter once and export GGUF:
#     model = PeftModel.from_pretrained(base, ADAPTER_DIR).merge_and_unload()
#   then run via llama.cpp. (Liquid publishes LFM2.5 GGUF builds.)
# ---------------------------------------------------------------------------
