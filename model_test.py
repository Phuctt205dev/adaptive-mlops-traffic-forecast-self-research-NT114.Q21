import pandas as pd
import numpy as np
from sklearn.metrics import mean_absolute_error, mean_squared_error

from src.preprocess import load_data, preprocess
from src.train import train_random_forest

# =========================
# 1. LOAD DATA
# =========================
df_raw = load_data("data/TrafficVolumeData.csv")

df_raw["date_time"] = pd.to_datetime(df_raw["date_time"])
# date_time = df_raw["date_time"].copy()

# =========================
# 2. PREPROCESS
# =========================
df = preprocess(df_raw)

# gắn lại date_time
# df["date_time"] = date_time

# =========================
# 3. SORT TIME
# =========================
df = df.sort_values("date_time")

# =========================
# 4. SPLIT TIME
# =========================
start_date = df["date_time"].min()

train_end = start_date + pd.DateOffset(months=23)
test_end = train_end + pd.DateOffset(months=3)

train_df = df[df["date_time"] < train_end]
test_df = df[(df["date_time"] >= train_end) & (df["date_time"] < test_end)]

print(f"📅 Train: {train_df['date_time'].min()} → {train_df['date_time'].max()}")
print(f"📅 Test : {test_df['date_time'].min()} → {test_df['date_time'].max()}")

# =========================
# 5. PREPARE DATA
# =========================
target = "traffic_volume"

X_train = train_df.drop([target, "date_time"], axis=1)
y_train = train_df[target]

X_test = test_df.drop([target, "date_time"], axis=1)
y_test = test_df[target]

# Fix lệch cột (nếu có)
X_test = X_test.reindex(columns=X_train.columns, fill_value=0)

# =========================
# 6. TRAIN
# =========================
model = train_random_forest(X_train, y_train)

# =========================
# 7. PREDICT
# =========================
y_pred = model.predict(X_test)

# =========================
# 8. EVALUATE
# =========================
mae = mean_absolute_error(y_test, y_pred)
mse = mean_squared_error(y_test, y_pred)
rmse = np.sqrt(mse)

# tránh chia cho 0
mape = np.mean(np.abs((y_test - y_pred) / (y_test + 1e-5))) * 100

print("\n📊 Evaluation:")
print(f"MAE  : {mae:.4f}")
print(f"MSE  : {mse:.4f}")
print(f"RMSE : {rmse:.4f}")
print(f"MAPE : {mape:.2f}%")

# =========================
# 9. SAVE RESULT
# =========================
result_df = pd.DataFrame({
    "date_time": test_df["date_time"],
    "y_true": y_test,
    "y_pred": y_pred,
    "error": y_test - y_pred
})

# result_df.to_csv("prediction_next_month.csv", index=False)

# print("\n✅ Saved prediction to prediction_next_month.csv")

result_path = "results/prediction_next_month.csv"
result_df.to_csv(result_path, index=False)

print(f"\n✅ Saved result to {result_path}")