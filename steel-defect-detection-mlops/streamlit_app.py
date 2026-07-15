"""
Steel Defect Detection - Streamlit Web Interface
Modern, kullanıcı dostu web arayüzü
"""

import streamlit as st
from ultralytics import YOLO
from PIL import Image
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from io import BytesIO
import time
import os

# Page config
st.set_page_config(
    page_title="Steel Defect Detection",
    page_icon="🔬",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS
st.markdown("""
    <style>
    .main {background-color: #f5f5f5;}
    .stAlert {background-color: #e3f2fd;}
    </style>
""", unsafe_allow_html=True)

# Model path
MODEL_PATH = "runs/detect/steel_defect_colab_50_epochs/weights/best.pt"

# Class names and colors
CLASS_INFO = {
    0: {"name": "Crazing", "color": "#FF6B6B"},
    1: {"name": "Inclusion", "color": "#FFA500"},
    2: {"name": "Patches", "color": "#FFD93D"},
    3: {"name": "Pitted Surface", "color": "#4CAF50"},
    4: {"name": "Rolled-in Scale", "color": "#2196F3"},
    5: {"name": "Scratches", "color": "#9C27B0"}
}

@st.cache_resource
def load_model():
    """Load YOLOv8 model (cached)"""
    try:
        model = YOLO(MODEL_PATH)
        return model, None
    except Exception as e:
        return None, str(e)

def predict_image(model, image, conf_threshold):
    """Run inference on image"""
    start_time = time.time()
    results = model.predict(
        source=image,
        conf=conf_threshold,
        save=False,
        verbose=False
    )[0]
    inference_time = (time.time() - start_time) * 1000
    return results, inference_time

def main():
    # Header
    st.title("Steel Surface Defect Detection System")
    st.markdown("### YOLOv8-based Real-time Quality Control")
    
    # Sidebar
    with st.sidebar:
        st.header("Settings")
        
        # Model info
        with st.expander("Model Information", expanded=True):
            st.metric("Model", "YOLOv8n")
            st.metric("mAP50", "76.47%")
            st.metric("Precision", "74.79%")
            st.metric("Recall", "69.39%")
        
        # Confidence threshold
        conf_threshold = st.slider(
            "Confidence Threshold",
            min_value=0.1,
            max_value=0.9,
            value=0.25,
            step=0.05,
            help="Lower = more sensitive, Higher = more precise"
        )
        
        # Defect classes
        with st.expander("Defect Classes"):
            for class_id, info in CLASS_INFO.items():
                st.markdown(f"**{info['name']}**")
    
    # Load model
    model, error = load_model()
    
    if error:
        st.error(f"Model loading failed: {error}")
        st.info(f"Expected model path: {MODEL_PATH}")
        return
    
    st.success("Model loaded successfully!")
    
    # Main content
    tab1, tab2, tab3 = st.tabs(["Single Image", "Batch Processing", "Analytics"])
    
    # TAB 1: Single Image Detection
    with tab1:
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("Upload Image")
            uploaded_file = st.file_uploader(
                "Choose a steel surface image",
                type=["jpg", "jpeg", "png"],
                help="Upload an image of steel surface for defect detection"
            )
            
            if uploaded_file:
                image = Image.open(uploaded_file)
                st.image(image, caption="Original Image", use_container_width=True)
                
                # Image info
                st.info(f"Size: {image.size[0]}x{image.size[1]} px")
        
        with col2:
            if uploaded_file:
                st.subheader("Detection Results")
                
                with st.spinner("Analyzing..."):
                    results, inference_time = predict_image(model, image, conf_threshold)
                
                # Show annotated image
                annotated_img = results.plot()
                annotated_img_pil = Image.fromarray(annotated_img)
                st.image(annotated_img_pil, caption="Detection Results", use_container_width=True)
                
                # Metrics
                col_a, col_b, col_c = st.columns(3)
                num_detections = len(results.boxes)
                
                col_a.metric("Detections", num_detections)
                col_b.metric("Speed (ms)", f"{inference_time:.1f}")
                col_c.metric("Status", "PASS" if num_detections == 0 else "DEFECT")
                
                # Detailed results
                if num_detections > 0:
                    st.warning(f"WARNING: {num_detections} defect(s) detected!")
                    
                    # Results table
                    detections_data = []
                    for i, box in enumerate(results.boxes):
                        class_id = int(box.cls[0])
                        conf = float(box.conf[0])
                        bbox = box.xyxy[0].tolist()
                        
                        detections_data.append({
                            "#": i + 1,
                            "Type": CLASS_INFO[class_id]["name"],
                            "Confidence": f"{conf*100:.1f}%",
                            "Location": f"({int(bbox[0])}, {int(bbox[1])})"
                        })
                    
                    df = pd.DataFrame(detections_data)
                    st.dataframe(df, use_container_width=True, hide_index=True)
                else:
                    st.success("No defects detected - Quality PASS")
    
    # TAB 2: Batch Processing
    with tab2:
        st.subheader("Batch Image Processing")
        
        uploaded_files = st.file_uploader(
            "Upload multiple images",
            type=["jpg", "jpeg", "png"],
            accept_multiple_files=True
        )
        
        if uploaded_files:
            st.info(f"{len(uploaded_files)} images uploaded")
            
            if st.button("Process All", type="primary"):
                progress_bar = st.progress(0)
                status_text = st.empty()
                
                batch_results = []
                
                for idx, file in enumerate(uploaded_files):
                    status_text.text(f"Processing {idx+1}/{len(uploaded_files)}: {file.name}")
                    
                    image = Image.open(file)
                    results, inf_time = predict_image(model, image, conf_threshold)
                    
                    batch_results.append({
                        "Filename": file.name,
                        "Detections": len(results.boxes),
                        "Status": "PASS" if len(results.boxes) == 0 else "DEFECT",
                        "Inference (ms)": f"{inf_time:.1f}"
                    })
                    
                    progress_bar.progress((idx + 1) / len(uploaded_files))
                
                status_text.text("Processing complete!")
                
                # Results summary
                df_batch = pd.DataFrame(batch_results)
                
                col1, col2, col3 = st.columns(3)
                col1.metric("Total Images", len(batch_results))
                col2.metric("PASS", len([r for r in batch_results if r["Status"] == "PASS"]))
                col3.metric("DEFECT", len([r for r in batch_results if r["Status"] == "DEFECT"]))
                
                st.dataframe(df_batch, use_container_width=True, hide_index=True)
                
                # Download results
                csv = df_batch.to_csv(index=False)
                st.download_button(
                    "Download Results (CSV)",
                    csv,
                    "batch_results.csv",
                    "text/csv"
                )
    
    # TAB 3: Analytics
    with tab3:
        st.subheader("Model Performance Analytics")
        
        # Load training results
        results_csv = "runs/detect/steel_defect_colab_50_epochs/results.csv"
        
        if os.path.exists(results_csv):
            df_train = pd.read_csv(results_csv)
            df_train = df_train.rename(columns=lambda x: x.strip())
            
            # Metrics over epochs
            fig = go.Figure()
            
            fig.add_trace(go.Scatter(
                x=df_train['epoch'],
                y=df_train['metrics/mAP50(B)'],
                name='mAP50',
                line=dict(color='#2196F3', width=2)
            ))
            
            fig.add_trace(go.Scatter(
                x=df_train['epoch'],
                y=df_train['metrics/precision(B)'],
                name='Precision',
                line=dict(color='#4CAF50', width=2)
            ))
            
            fig.add_trace(go.Scatter(
                x=df_train['epoch'],
                y=df_train['metrics/recall(B)'],
                name='Recall',
                line=dict(color='#FF9800', width=2)
            ))
            
            fig.update_layout(
                title="Training Metrics over Epochs",
                xaxis_title="Epoch",
                yaxis_title="Score",
                hovermode='x unified',
                height=400
            )
            
            st.plotly_chart(fig, use_container_width=True)
            
            # Loss curves
            fig_loss = go.Figure()
            
            fig_loss.add_trace(go.Scatter(
                x=df_train['epoch'],
                y=df_train['train/box_loss'],
                name='Box Loss',
                line=dict(color='#FF6B6B')
            ))
            
            fig_loss.add_trace(go.Scatter(
                x=df_train['epoch'],
                y=df_train['train/cls_loss'],
                name='Class Loss',
                line=dict(color='#9C27B0')
            ))
            
            fig_loss.update_layout(
                title="Training Loss",
                xaxis_title="Epoch",
                yaxis_title="Loss",
                hovermode='x unified',
                height=400
            )
            
            st.plotly_chart(fig_loss, use_container_width=True)
        else:
            st.warning("Training results not found")
    
    # Footer
    st.markdown("---")
    st.markdown(
        """
        <div style='text-align: center; color: #666;'>
        <p><b>Steel Defect Detection System v1.0</b></p>
        <p>Powered by YOLOv8n | Training: 50 epochs | Dataset: NEU-DET</p>
        </div>
        """,
        unsafe_allow_html=True
    )

if __name__ == "__main__":
    main()
