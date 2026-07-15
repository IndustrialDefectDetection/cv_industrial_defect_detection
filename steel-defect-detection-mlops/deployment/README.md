# 🚀 Docker Deployment - Steel Defect Detection

## Quick Start

### 1️⃣ Build ve Başlat (Tek Komut)

```bash
docker-compose -f deployment/docker-compose.yml up --build
```

### 2️⃣ Servisler Hazır!

- **API**: http://localhost:8080
- **MLflow**: http://localhost:5000
- **Docs**: http://localhost:8080/docs (Swagger UI)

---

## API Kullanımı

### Test - Single Image

```bash
curl -X POST "http://localhost:8080/predict" \
  -F "file=@data/dataset/images/valid/crazing_1.jpg" \
  -F "confidence=0.25"
```

### Batch Prediction

```bash
curl -X POST "http://localhost:8080/batch-predict" \
  -F "files=@image1.jpg" \
  -F "files=@image2.jpg" \
  -F "confidence=0.3"
```

### Model Info

```bash
curl http://localhost:8080/model-info
```

### Health Check

```bash
curl http://localhost:8080/health
```

---

## PowerShell İçin Test Komutları

```powershell
# Health check
Invoke-RestMethod -Uri "http://localhost:8080/health"

# Model info
Invoke-RestMethod -Uri "http://localhost:8080/model-info"

# Prediction
$image = Get-Item "data\dataset\images\valid\crazing_1.jpg"
$form = @{
    file = $image
    confidence = 0.25
}
Invoke-RestMethod -Uri "http://localhost:8080/predict" -Method Post -Form $form
```

---

## Container Yönetimi

```bash
# Başlat
docker-compose -f deployment/docker-compose.yml up -d

# Durdur
docker-compose -f deployment/docker-compose.yml down

# Log'ları izle
docker-compose -f deployment/docker-compose.yml logs -f

# Sadece API'yi yeniden başlat
docker-compose -f deployment/docker-compose.yml restart defect-api
```

---

## Performans

- **Inference Speed**: ~10-15ms/image (CPU)
- **Throughput**: ~50-100 req/sec
- **Memory**: ~500MB (model + API)
