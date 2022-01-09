@echo off

@rem DIVISION 1. PERSONS [38 - 86]
for %%i in (1 2 2.5 2.52 2.53 2.55 2.57 2.6 2.7 2.9) do py scraper.py 1 %%i

@rem DIVISION 2. PROPERTY [654 - 1422]
for %%i in (1 2 3 4) do py scraper.py 2 %%i

@rem DIVISION 3. OBLIGATIONS [1427 - 3273.16]
for %%i in (1 2 3 4) do py scraper.py 3 %%i

@rem DIVISION 4. GENERAL PROVISIONS [3274 - 9566]
for %%i in (1 2 3 4 5 5.3 5.5 6) do py scraper.py 4 %%i
