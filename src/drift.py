import numpy as np

def detect_drift(old_data, new_data, threshold=0.1):
    old_mean = np.mean(old_data)
    new_mean = np.mean(new_data)

    old_std = np.std(old_data)
    new_std = np.std(new_data)

    mean_diff = abs(old_mean - new_mean) / (abs(old_mean) + 1e-6)
    std_diff = abs(old_std - new_std) / (abs(old_std) + 1e-6)

    print("\n=== DRIFT CHECK ===")
    print(f"Old mean: {old_mean:.2f} | New mean: {new_mean:.2f}")
    print(f"Mean diff: {mean_diff:.4f}")

    print(f"Old std: {old_std:.2f} | New std: {new_std:.2f}")
    print(f"Std diff: {std_diff:.4f}")

    if mean_diff > threshold or std_diff > threshold:
        print("🚨 DRIFT DETECTED")
        return True
    else:
        print("✅ NO DRIFT")
        return False