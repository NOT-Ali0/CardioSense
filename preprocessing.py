import numpy as np
from scipy.signal import butter, filtfilt

MAX_LEN = 3000  # الأبعاد الصارمة والمثالية المطلقة لمدخلات معمارية الـ SE-ResNet1D

def preprocess_signal(signal, fs=300):
    """
    Advanced Biomedical Signal Processing Pipeline for SCAI CardioSense AI.
    Optimized for Hardware-Timer sample arrays (Strict 3000 points).
    """
    # 1. التحويل الصارم لبيانات المصفوفة إلى تنسيق عشري عالي الدقة (Single-Precision Float)
    signal = np.array(signal, dtype=np.float32).flatten()
    
    # 2. الحماية الرياضية ضد الانقطاع: إذا كانت الإشارة فارغة تماماً أو ميتة
    if len(signal) == 0 or np.all(signal == 0):
        print("🚨 [CRITICAL] Dead signal node detected. Injecting baseline vector.")
        return np.zeros((1, 1, MAX_LEN), dtype=np.float32)

    # 3. إزالة الانزياح المستمر (Baseline Wander Removal via DC Offset Cancellation)
    # طرح المتوسط الحسابي لمركزة الإشارة فسيولوجياً حول الصفر الفولتائي
    signal = signal - np.mean(signal)
    
    # 4. 🌟 [التحديث الجوهري لحفظ النويز]: فلتر Butterworth طبي مرن (0.5Hz - 45Hz)
    # تم رفع التردد العلوي وخفض الرتبة إلى (2nd Order) لمنع نحت وتنعيم الشوشرة الحركية (Muscle Artifacts)
    # هذا يضمن أن حركة اليد تظل تظهر كاضطراب فوضوي يرفضه الحارس أو يصنفه الاستشاري بدقة
    nyquist = 0.5 * fs
    low_cutoff = 0.5 / nyquist   
    high_cutoff = 45.0 / nyquist 
    
    b, a = butter(2, [low_cutoff, high_cutoff], btype='band')
    
    # تطبيق الفلترة الصفرية الطورية (Zero-Phase Filtering) لمنع حدوث زحزحة أو تشويه في زمن موجات P-QRS-T
    signal = filtfilt(b, a, signal)

    # 5. 📐 [حارس الأبعاد المطلق]: مطابقة الداتا القادمة من الـ HTML المقيد بعدد العينات
    signal_len = len(signal)
    
    if signal_len == MAX_LEN:
        # الحالة المثالية القياسية: المنظومة متزامنة 100%
        pass
    elif signal_len > MAX_LEN:
        # في حال وجود عينات زائدة ميكروية ناتجة عن تأخر إغلاق الجلسة: نقوم بالقص المركزي النظيف
        start_idx = (signal_len - MAX_LEN) // 2
        signal = signal[start_idx : start_idx + MAX_LEN]
        print(f"✂️ [DSP CLIPPER] Central crop applied. Retained strict {MAX_LEN} samples from {signal_len}.")
    else:
        # خط الدفاع الأخير لحماية الـ Network من الـ Crash في الأوقات الحرجة
        print(f"⚠️ [DSP PADDING] Missing data ({signal_len}/3000). Securing boundaries.")
        signal = np.pad(signal, (0, MAX_LEN - signal_len), mode='constant', constant_values=0.0)

    # 6. التقييس القياسي الصارم (Z-score Normalization)
    # توحيد السعة (Amplitude) لإلغاء الفوارق بين جلود المرضى ومقاومة الأقطاب، مع حماية ضد الانقسام على صفر
    std_val = np.std(signal)
    signal = (signal - np.mean(signal)) / (std_val if std_val > 1e-6 else 1.0)

    # 7. إعادة التشكيل ثلاثي الأبعاد المتوافق بالملي مع أوزان الـ PyTorch Tensor [Batch, Channels, Length]
    return signal.reshape(1, 1, MAX_LEN)