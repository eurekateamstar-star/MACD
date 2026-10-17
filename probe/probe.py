import sys; sys.path.insert(0, ".")
import fundamentals as fm
r = fm.fetch_ptt({"2330": "台積電", "2317": "鴻海", "2454": "聯發科", "2603": "長榮", "3231": "緯創", "2382": "廣達", "2308": "台達電", "3017": "奇鋐"}, max_pages=80)
open("probe/result.txt", "w", encoding="utf-8").write(repr({k: (v["posts"], v["push"], v["items"][0]["t"]) for k, v in r.items()}))
