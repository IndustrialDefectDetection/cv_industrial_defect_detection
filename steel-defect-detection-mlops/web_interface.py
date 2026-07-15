"""
Steel Defect Detection - Web Interface
Basit drag-drop arayüz ile defect detection
"""

import gradio as gr
from ultralytics import YOLO
import os

# Model yükle
MODEL_PATH = "runs/detect/steel_defect_colab_50_epochs/weights/best.pt"
model = YOLO(MODEL_PATH)

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
    
    # Inference yap
    results = model.predict(
        source=image,
        conf=confidence_threshold,
        save=False,
        verbose=False
    )[0]
    
    # Tespit edilen defect sayısı
    num_detections = len(results.boxes)
    
    # Sonuç mesajı
    if num_detections == 0:
        message = f"✅ Kusur tespit edilmedi! (Eşik: {confidence_threshold})"
    else:
        message = f"⚠️ {num_detections} adet kusur tespit edildi:\n\n"
        
        # Her defect için detay
        for i, box in enumerate(results.boxes):
            class_id = int(box.cls[0])
            confidence = float(box.conf[0])
            class_name = CLASS_NAMES.get(class_id, "Bilinmeyen")
            message += f"{i+1}. {class_name} - Güven: %{confidence*100:.1f}\n"
    
    # İşaretlenmiş görüntü
    annotated_image = results.plot()
    
    return annotated_image, message


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
        inbrowser=True  # Otomatik tarayıcıda aç
    )
