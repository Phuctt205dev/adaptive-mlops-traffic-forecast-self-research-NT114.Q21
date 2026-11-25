# Synthetic Hanoi traffic dataset

`TrafficVolumeData_Hanoi_2026_2030_synthetic.csv` contains one row per hour
from 2026-01-01 00:00:00 through 2030-12-31 23:00:00 in local Hanoi time.
Every row is **simulated**, including 2026; the file contains neither measured
Hanoi traffic nor real future observations or forecasts.

Run `python data/synthetic/generate_hanoi_synthetic.py` to reproduce it (seed
`20261003`). The generator starts with the corresponding hours of
`data/synthetic/TrafficVolumeData_HCM_2015_2030_synthetic.csv`, which itself is based on
the hourly traffic patterns in `data/TrafficVolumeData.csv`. It creates
independent daily traffic variation, modest weekday commute peaks, and assumed
four-season weather: cooler winter, hot summer, and more rain from May through
October. Temperatures are in Kelvin. Fixed-date holiday labels come from the
source synthetic file; Tet is not modeled.

The 15-column schema matches the source and the website upload contract. Use
this dataset for integration or load testing, not for claims about observed
Hanoi traffic or model accuracy.
