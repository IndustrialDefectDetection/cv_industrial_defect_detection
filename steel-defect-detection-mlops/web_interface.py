"""
Steel Defect Detection - Web Interface
Basit drag-drop arayüz ile defect detection
"""

from __future__ import annotations

import logging
import math
import os
import threading
from pathlib import Path

from PIL import Image

# Capture Pillow's real decoder before Ultralytics installs its fallback
# wrapper, which may try to install optional packages for malformed uploads.
from streamlit_image_security import restore_safe_pillow_open

import gradio as gr
from ultralytics import YOLO

from deployment.model_integrity import verify_model_integrity

# Gradio decodes uploads before calling detect_defects(), so the process-wide
# Pillow entry point itself must be restored before the server starts.
restore_safe_pillow_open()


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent
MAX_IMAGE_PIXELS = 16_000_000
# Pillow raises (rather than only warns) above twice this value. Set its
# decoder guard to half our application limit so oversized uploads are stopped
# before Gradio materializes their full pixel buffer.
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS // 2
INFERENCE_SLOT = threading.BoundedSemaphore(value=1)

# Model yükle
MODEL_PATH = Path(
    os.getenv(
        "MODEL_PATH",
        str(
            PROJECT_ROOT
            / "runs/detect/steel_defect_colab_50_epochs/weights/best.pt"
        ),
    )
)
model = YOLO(str(verify_model_integrity(MODEL_PATH)))

# Defect class isimleri
CLASS_NAMES = {
    0: "Crazing (Çatlak)",
    1: "Inclusion (Yabancı Madde)",
    2: "Patches (Yama)",
    3: "Pitted Surface (Çukurlu Yüzey)",
    4: "Rolled-in Scale (Rulo Kusuru)",
    5: "Scratches (Çizik)"
}

def detect_defects(image, confidence_threshold):
    """
    Görüntüdeki defect'leri tespit et
    """
    if image is None:
        return None, "❌ Lütfen bir görüntü yükleyin!"

    if not isinstance(image, Image.Image):
        return None, "❌ Yüklenen dosya geçerli bir görüntü değil."
    if image.format not in {"JPEG", "PNG"}:
        return None, "❌ Yalnızca JPEG ve PNG görüntüleri desteklenir."
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        return None, "❌ Görüntü boyutları izin verilen sınırı aşıyor."

    try:
        threshold = float(confidence_threshold)
    except (TypeError, ValueError):
        return None, "❌ Güven eşiği geçersiz."
    if not math.isfinite(threshold) or not 0.1 <= threshold <= 0.9:
        return None, "❌ Güven eşiği geçersiz."

    if not INFERENCE_SLOT.acquire(blocking=False):
        return None, "⏳ Sistem meşgul. Lütfen biraz sonra tekrar deneyin."

    try:
        # Gradio already bounds the upload bytes at the HTTP layer below.
        # Materialize a plain RGB copy so inference never follows lazy file I/O.
        safe_image = image.convert("RGB").copy()
        results = model.predict(
            source=safe_image,
            conf=threshold,
            max_det=100,
            save=False,
            verbose=False,
        )[0]

        num_detections = len(results.boxes)
        if num_detections == 0:
            message = f"✅ Kusur tespit edilmedi! (Eşik: {threshold})"
        else:
            message = f"⚠️ {num_detections} adet kusur tespit edildi:\n\n"
            for i, box in enumerate(results.boxes):
                class_id = int(box.cls[0])
                confidence = float(box.conf[0])
                class_name = CLASS_NAMES.get(class_id, "Bilinmeyen")
                message += (
                    f"{i + 1}. {class_name} - Güven: "
                    f"%{confidence * 100:.1f}\n"
                )

        return results.plot(), message
    except Exception:
        LOGGER.exception("Gradio inference failed")
        return None, "❌ Görüntü işlenemedi. Lütfen tekrar deneyin."
    finally:
        INFERENCE_SLOT.release()


# Gradio interface
with gr.Blocks(title="🔍 Çelik Kusur Tespit Sistemi", theme=gr.themes.Soft()) as demo:
    
    gr.Markdown(
        """
        # 🏭 Çelik Yüzey Kusur Tespit Sistemi
        ### YOLOv8 ile Gerçek Zamanlı Defect Detection
        
        📸 **Kullanım:** Çelik yüzey görüntüsü yükleyin, sistem otomatik kusurları tespit eder.
        """
    )
    
    with gr.Row():
        with gr.Column():
            # Input
            input_image = gr.Image(
                type="pil",
                label="📤 Çelik Yüzey Görüntüsü Yükleyin",
                height=400
            )
            
            confidence_slider = gr.Slider(
                minimum=0.1,
                maximum=0.9,
                value=0.25,
                step=0.05,
                label="🎯 Güven Eşiği (Confidence Threshold)",
                info="Düşük değer = daha hassas, yüksek değer = daha kesin"
            )
            
            detect_btn = gr.Button("🔍 Kusur Tespiti Yap", variant="primary", size="lg")
            
            gr.Markdown(
                """
                ### 📋 Tespit Edilebilir Kusurlar:
                - 🔴 Crazing (Çatlak desenler)
                - 🟠 Inclusion (Yabancı madde)
                - 🟡 Patches (Yüzey yamaları)
                - 🟢 Pitted Surface (Çukurlu yüzey)
                - 🔵 Rolled-in Scale (Rulo kusurları)
                - 🟣 Scratches (Çizikler)
                """
            )
        
        with gr.Column():
            # Output
            output_image = gr.Image(
                label="📊 Tespit Sonucu (İşaretlenmiş Görüntü)",
                height=400
            )
            
            output_message = gr.Textbox(
                label="📝 Detaylı Rapor",
                lines=10,
                max_lines=15
            )
    
    # Örnek görüntüler
    gr.Examples(
        examples=[
            ["data/dataset/images/valid"],
        ],
        inputs=input_image,
        label="📁 Örnek Test Görüntüleri Yükle"
    )
    
    # Event handler
    detect_btn.click(
        fn=detect_defects,
        inputs=[input_image, confidence_slider],
        outputs=[output_image, output_message]
    )
    
    gr.Markdown(
        """
        ---
        **Model:** YOLOv8n | **Accuracy:** 76.47% mAP50 | **Inference:** ~10ms
        """
    )


# Uygulamayı başlat
if __name__ == "__main__":
    print("=" * 60)
    print("🚀 Çelik Kusur Tespit Web Arayüzü Başlatılıyor...")
    print("=" * 60)
    print(f"📦 Model: {MODEL_PATH}")
    print("🌐 Arayüz açılıyor...")
    print("=" * 60)
    
    demo.launch(
        server_name="127.0.0.1",
        server_port=7860,
        share=False,
        inbrowser=True,  # Otomatik tarayıcıda aç
        max_file_size="10mb",
        enable_monitoring=False,
        strict_cors=True,
        show_error=False,
    )
