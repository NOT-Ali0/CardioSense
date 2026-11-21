import os
import json
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from preprocessing import preprocess_signal

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# قراءة الأوزان من نفس المجلد
MODEL_PATH = os.path.join(BASE_DIR, "stanford_ecg_model.pth")

# قيم العتبات السريرية التي نتجت عن تدريب نموذجك
TEMP = 1.4723
CONF_TH = 0.95
ENT_TH = 0.400

def entropy(prob):
    """
    حساب درجة التشتت واللايقين في قرار الشبكة العصبية
    """
    p = np.clip(prob, 1e-7, 1 - 1e-7)
    return -(p * np.log2(p) + (1 - p) * np.log2(1 - p))

# --- Architecture SE-ResNet1D ---
class SEBlock(nn.Module):
    def __init__(self, channel, reduction=16):
        super().__init__()
        self.avg_pool = nn.AdaptiveAvgPool1d(1)
        self.fc = nn.Sequential(
            nn.Linear(channel, channel // reduction, bias=False),
            nn.ReLU(),
            nn.Linear(channel // reduction, channel, bias=False),
            nn.Sigmoid()
        )
    def forward(self, x):
        b, c, _ = x.size()
        y = self.avg_pool(x).view(b, c)
        y = self.fc(y).view(b, c, 1)
        return x * y.expand_as(x)

class SEResNetBlock1D(nn.Module):
    def __init__(self, in_channels, out_channels, stride=1):
        super().__init__()
        self.conv1 = nn.Conv1d(in_channels, out_channels, kernel_size=7, stride=stride, padding=3)
        self.bn1 = nn.BatchNorm1d(out_channels)
        self.conv2 = nn.Conv1d(out_channels, out_channels, kernel_size=7, padding=3)
        self.bn2 = nn.BatchNorm1d(out_channels)
        self.relu = nn.ReLU()
        self.se = SEBlock(out_channels)
        self.shortcut = nn.Sequential()
        if stride != 1 or in_channels != out_channels:
            self.shortcut = nn.Sequential(
                nn.Conv1d(in_channels, out_channels, 1, stride=stride),
                nn.BatchNorm1d(out_channels)
            )
    def forward(self, x):
        residual = self.shortcut(x)
        x = self.relu(self.bn1(self.conv1(x)))
        x = self.bn2(self.conv2(x))
        x = self.se(x)
        x += residual
        return self.relu(x)

class Medical_ECG_Net(nn.Module):
    def __init__(self):
        super().__init__()
        self.stem = nn.Sequential(
            nn.Conv1d(1, 32, 15, stride=2, padding=7),
            nn.BatchNorm1d(32), nn.ReLU(), nn.MaxPool1d(2)
        )
        self.layer1 = SEResNetBlock1D(32, 64, stride=2)
        self.layer2 = SEResNetBlock1D(64, 128, stride=2)
        self.layer3 = SEResNetBlock1D(128, 256, stride=2)
        self.layer4 = SEResNetBlock1D(256, 256)
        self.pool = nn.AdaptiveAvgPool1d(1)
        self.classifier = nn.Sequential(
            nn.Linear(256, 64), nn.ReLU(), nn.Dropout(0.4), nn.Linear(64, 1)
        )
    def forward(self, x):
        x = self.stem(x)
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)
        x = self.pool(x).squeeze(-1)
        return self.classifier(x).squeeze(-1)

# تحميل النموذج في الذاكرة لمرة واحدة عند تشغيل السيرفر
DEVICE = torch.device("cpu")
model = Medical_ECG_Net()

if os.path.exists(MODEL_PATH):
    model.load_state_dict(torch.load(MODEL_PATH, map_location=DEVICE))
    model.eval()
else:
    print(f"🚨 [CRITICAL ERROR]: Production Model not found at {MODEL_PATH}")

def predict(signal):
    """
    محرك الاستدلال وتوليد الخرائط الحرارية (Inference & XAI Engine)
    """
    x = preprocess_signal(signal)
    x = torch.tensor(x, dtype=torch.float32)
    
    with torch.no_grad():
        # 1. التمرير المرحلي للوصول للطبقات العميقة
        f1 = model.stem(x)
        f2 = model.layer1(f1)
        f3 = model.layer2(f2)
        f4 = model.layer3(f3)
        features = model.layer4(f4) # أبعادها: [1, 256, L]
        
        # 2. استكمال التمرير للوصول للقرار النهائي
        pooled = model.pool(features).squeeze(-1)
        logits = model.classifier(pooled).squeeze(-1).item()
        
        # 3. توليد مصفوفة XAI الحقيقية المبدئية بأبعاد صحيحة 100%
        # حساب متوسط القنوات ثم تحويلها إلى [1, 1, L]
        activation_map = torch.mean(features, dim=1).unsqueeze(1) 
        
        # توسيع المصفوفة (Interpolation) لتتطابق مع أبعاد الإشارة الأصلية 3000
        activation_map = F.interpolate(
            activation_map, size=3000, mode='linear', align_corners=False
        ).squeeze().numpy()
        
        # تسوية قيم التنشيط بين 0.0 و 1.0 (Normalization)
        map_min, map_max = np.min(activation_map), np.max(activation_map)
        if (map_max - map_min) > 1e-8:
            activation_map = (activation_map - map_min) / (map_max - map_min)
        else:
            activation_map = np.zeros_like(activation_map)
            
        xai_list = [float(val) for val in activation_map]

    # 4. حساب الثقة والإنتروبيا بمعامل الحرارة
    prob = 1 / (1 + np.exp(-logits / TEMP))
    confidence = max(prob, 1 - prob)
    ent = entropy(prob)

    # 5. الفرز والتصعيد للحالات غير المؤكدة
    if confidence < CONF_TH or ent > ENT_TH:
        return {
            "prediction": "UNCERTAIN", 
            "probability": float(prob), 
            "confidence": float(confidence),
            "xai_weights": xai_list
        }

    # 6. التشخيص النهائي الواثق
    prediction = "AFIB" if prob >= 0.5 else "NORMAL"
    return {
        "prediction": prediction,
        "probability": float(prob),
        "confidence": float(confidence),
        "entropy": float(ent),
        "xai_weights": xai_list
    }
