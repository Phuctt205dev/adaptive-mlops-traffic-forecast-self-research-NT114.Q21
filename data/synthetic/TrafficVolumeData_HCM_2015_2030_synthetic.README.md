# Synthetic Ho Chi Minh City traffic dataset

`TrafficVolumeData_HCM_2015_2030_synthetic.csv` contains one row per hour from
2015-01-01 00:00:00 through 2030-12-31 23:00:00 (local Ho Chi Minh City time).
All rows are **simulated**, including the historical years. The file contains
no observed Ho Chi Minh City traffic measurements or real future forecasts.

The reproducible generator is `data/synthetic/generate_hcm_synthetic.py` (seed
`20261002`). It samples complete days from `data/TrafficVolumeData.csv` by
month and weekday, preserving the source's hourly traffic shape. It applies
small simulated year, weather, and holiday adjustments. Weather is transformed
to a tropical pattern: no snow, a wetter May-November season, and temperatures
in Kelvin. It marks four fixed-date Vietnamese holidays; Tet is omitted.

The 15-column schema matches the source CSV and the website's dataset upload
contract. This dataset is suitable for integration and load testing, not for
claims about actual Ho Chi Minh City traffic or model accuracy.
