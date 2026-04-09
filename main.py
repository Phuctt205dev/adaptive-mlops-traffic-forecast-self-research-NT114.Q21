import pandas as pd
import time
import os
import joblib  # để save/load model

from src.preprocess import load_data, preprocess
from src.train import train_model
from src.drift import detect_drift

from sklearn.metrics import mean_absolute_error

# =========================
# ⚙️ CONFIG
# =========================
WINDOW_SIZE = 90 * 24      # 3 tháng
BATCH_SIZE = 24 * 7        # 1 tuần
MODEL_DIR = "models"
MODEL_FILE = os.path.join(MODEL_DIR, "traffic_model.pkl")

os.makedirs(MODEL_DIR, exist_ok=True)

# =========================
# 1. LOAD & PREPROCESS
# =========================
df = load_data("data/TrafficVolumeData.csv")
df = preprocess(df)

# =========================
# 2. KHỞI TẠO DATA BAN ĐẦU
# =========================
current_data = df[:WINDOW_SIZE]

X = current_data.drop("traffic_volume", axis=1)
y = current_data["traffic_volume"]

print("🚀 Initial training...")
model = train_model(X, y)

# save initial model
joblib.dump(model, MODEL_FILE)
print(f"💾 Initial model saved to {MODEL_FILE}")

# =========================
# 3. STREAMING SIMULATION
# =========================
start = WINDOW_SIZE
prev_batch = None   # dùng để evaluate chuẩn

while start < len(df):
    print("\n📦 New batch arrived...")

    new_data = df[start:start + BATCH_SIZE]

    # =========================
    # 📊 EVALUATE (TRÊN BATCH TRƯỚC)
    # =========================
    if prev_batch is not None:
        X_test = prev_batch.drop("traffic_volume", axis=1)
        y_test = prev_batch["traffic_volume"]

        y_pred = model.predict(X_test)
        mae = mean_absolute_error(y_test, y_pred)
        mape = (abs(y_test - y_pred) / y_test).mean()

        print(f"📊 MAE (previous batch): {mae:.2f}")
        print(f"📊 MAPE: {mape:.2%}")

    # =========================
    # 🚨 DRIFT DETECTION
    # =========================
    drift = detect_drift(
        current_data["traffic_volume"],
        new_data["traffic_volume"]
    )

    # =========================
    # 🔁 RETRAIN NẾU CÓ DRIFT
    # =========================
    if drift:
        print("🔁 Retraining model...")

        updated_data = pd.concat([current_data, new_data])
        updated_data = updated_data.tail(WINDOW_SIZE)

        X_new = updated_data.drop("traffic_volume", axis=1)
        y_new = updated_data["traffic_volume"]

        model = train_model(X_new, y_new)
        current_data = updated_data

        # save model sau mỗi lần retrain
        joblib.dump(model, MODEL_FILE)
        print(f"✅ Model updated and saved to {MODEL_FILE}")

    else:
        print("✅ No retrain")
        current_data = pd.concat([current_data, new_data]).tail(WINDOW_SIZE)

    # =========================
    # UPDATE BATCH
    # =========================
    prev_batch = new_data

    # next batch
    start += BATCH_SIZE

    print("⏳ Waiting for next week (10s)...\n")
    time.sleep(10)

print("\n🎯 Pipeline finished")