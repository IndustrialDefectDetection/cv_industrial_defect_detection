# 🚀 COMPLETE DEPLOYMENT GUIDE

## 📦 FULL STACK - ALL SERVICES

### Servisler:

1. **FastAPI** (Port 8080) - REST API
2. **Streamlit** (Port 8501) - Web UI
3. **MLflow** (Port 5000) - Experiment Tracking
4. **Prometheus** (Port 9090) - Metrics
5. **Grafana** (Port 3000) - Dashboards
6. **Node Exporter** (Port 9100) - System Metrics

---

## 🎯 HIZLI BAŞLATMA

### SEÇENEK 1: Streamlit Web UI (En Kolay)

```powershell
# Gerekli paketleri yükle
pip install streamlit plotly

# Web UI'ı başlat
streamlit run streamlit_app.py
```

**Tarayıcıda:** http://localhost:8501

---

### SEÇENEK 2: Full Stack (Docker - TÜM SERVİSLER)

```powershell
docker-compose -f deployment/docker-compose.full.yml up --build
```

**Erişim:**

- 🌐 Streamlit UI: http://localhost:8501
- 🔌 FastAPI: http://localhost:8080/docs
- 📊 MLflow: http://localhost:5000
- 📈 Prometheus: http://localhost:9090
- 📊 Grafana: http://localhost:3000 (admin/admin)

---

## 📊 GRAFANA DASHBOARD KURULUMU

1. Grafana aç: http://localhost:3000
2. Login: `admin` / `admin`
3. Add Data Source → Prometheus
   - URL: `http://prometheus:9090`
4. Import Dashboard:
   - Dashboard ID: 1860 (Node Exporter Full)
   - Dashboard ID: 11074 (API Metrics)

---

## 🎨 STREAMLIT FEATURES

✅ Drag-drop image upload
✅ Real-time detection visualization
✅ Confidence threshold adjustment
✅ Batch processing
✅ Training analytics with interactive charts
✅ Export results to CSV
✅ Detailed defect reports

---

## 📈 PROMETHEUS METRICS

Tracked metrics:

- `api_requests_total` - Total API requests
- `inference_duration_seconds` - Inference time
- `detections_total` - Defects by class
- `http_request_duration_seconds` - Request latency

---

## 🔧 DEVELOPMENT MODE

### Lokal Streamlit (Docker olmadan):

```powershell
streamlit run streamlit_app.py
```

### Lokal FastAPI (Docker olmadan):

```powershell
pip install prometheus-client
uvicorn deployment.api:app --reload --port 8080
```

---

## 📦 PRODUCTION DEPLOYMENT

### AWS EC2:

```bash
# Docker ile deploy
docker-compose -f deployment/docker-compose.full.yml up -d

# NGINX reverse proxy ekle
# SSL certificate (Let's Encrypt)
```

### Kubernetes:

```bash
kubectl apply -f deployment/k8s/
```

---

## 🎯 SUNUMDA GÖSTER

1. **Streamlit UI**: Drag-drop ile görüntü yükle → detection göster
2. **Grafana**: Real-time metrics dashboard
3. **MLflow**: Training history ve model versioning
4. **FastAPI Swagger**: Interactive API documentation

---

## 🚨 TROUBLESHOOTING

**Port conflict:**

```powershell
# Kullanılan portları kontrol et
netstat -ano | findstr :8501
```

**Docker build hatası:**

```powershell
# Cache temizle
docker system prune -a
```

---

## 📝 REQUIREMENTS UPDATE

Projeye ekle:

```txt
streamlit>=1.28.0
plotly>=5.17.0
prometheus-client>=0.19.0
```
