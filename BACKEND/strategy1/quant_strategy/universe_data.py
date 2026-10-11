"""Stock-pool data: thematic ETFs, factor ETFs and a few constants. Edit the lists here, the settings are in main.py CONFIG."""

DATA_VERSION = 5

ETFS = ['ROBO', 'XBI', 'BUG', 'GRID', 'UFO', 'NLR', 'XAR', 'IYW', 'SOXX']   # thematic ETFs
# Extra tech sleeves (CONFIG 'extra_tech_etfs'). Each sleeve is a peer group: its top `leaders_per_group` constituents are the leaders. Launch year in brackets: before it
# the sleeve has no constituents (a sleeve with no data just contributes nothing; the log shows ETF_LEADERS lines for every sleeve and year).
EXTRA_TECH_ETFS = ['IGV',    # iShares Expanded Tech-Software            (2001)
                   'SMH',    # VanEck Semiconductor                       (2000)
                   'SKYY',   # First Trust Cloud Computing                (2011)
                   'FDN',    # First Trust Dow Jones Internet             (2006)
                   'HACK',   # Amplify Cybersecurity (older than BUG)     (2014)
                   'BOTZ',   # Global X Robotics & Artificial Intelligence (2016)
                   'FINX']   # Global X FinTech                           (2016)

EXTRA_SPARE = 7   # extra ETFs subscribe leaders_per_group + EXTRA_SPARE top constituents so a duplicate leader can be replaced by the next-ranked one

FACTORS = ['SPY', 'IWM', 'QQQ']
BUFFER = 0.95                                                                   # share of free cash/margin an order may use
