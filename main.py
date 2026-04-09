import pandas as pd

from src.preprocess import load_data, preprocess
from src.train import train_model
from src.drift import detect_drift

from sklearn.metrics import mean_absolute_error

# =========================
# ⚙️ CONFIG
# =========================
WINDOW_SIZE = 20000
BATCH_SIZE = 2000

# =========================
# 1. LOAD & PREPROCESS
# =========================
df = load_data("data/TrafficVolumeData.csv")
df = preprocess(df)

# =========================
# 2. KHỞI TẠO DATA BAN ĐẦU
# =========================
current_data = df[:WINDOW_SIZE]

# train model lần đầu
X = current_data.drop("traffic_volume", axis=1)
y = current_data["traffic_volume"]

print("🚀 Initial training...")
model = train_model(X, y)

# =========================
# 3. GIẢ LẬP DATA STREAMING
# =========================
start = WINDOW_SIZE

while start < len(df):
    print("\n📦 New batch arrived...")

    # lấy batch mới
    new_data = df[start:start + BATCH_SIZE]

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

        # 🔥 SLIDING WINDOW
        updated_data = updated_data.tail(WINDOW_SIZE)

        X_new = updated_data.drop("traffic_volume", axis=1)
        y_new = updated_data["traffic_volume"]

        model = train_model(X_new, y_new)

        current_data = updated_data

        print("✅ Model updated")

    else:
        print("✅ No retrain")

        # vẫn update data nhưng không train
        current_data = pd.concat([current_data, new_data]).tail(WINDOW_SIZE)

    # =========================
    # 📊 (OPTIONAL) EVALUATE NHANH
    # =========================
    X_test = new_data.drop("traffic_volume", axis=1)
    y_test = new_data["traffic_volume"]

    y_pred = model.predict(X_test)
    mae = mean_absolute_error(y_test, y_pred)

    print(f"MAE on new batch: {mae:.2f}")

    # next batch
    start += BATCH_SIZE

print("\n🎯 Pipeline finished")