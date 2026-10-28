"""Non-generative deployment checks against the completed Wikipedia index."""

import json, os, time
from pathlib import Path


def check(folder, encoder=None):
    from wiki_retrieval import search_wikipedia, wikipedia_article

    folder = Path(folder)
    os.environ["WIKIPEDIA_DATA_DIR"] = str(folder)
    report = {"queries": []}
    for query, expected in [
        ("Казанский кремль", "кремль"),
        ("Мечеть Кул-Шариф в Казани", "кул"),
        ("Старая Татарская слобода в Казани", "старая татарская слобода"),
    ]:
        start = time.monotonic()
        result = search_wikipedia(query, limit=6, folder=folder, encoder=encoder)
        titles = [r["title"] for r in result["results"]]
        if not titles or expected not in titles[0].lower():
            raise ValueError(f"Retrieval check failed: {query}, {titles}")
        if any(
            not r["text"] or not r["revision_url"].startswith("https://ru.wikipedia.org/w/index.php?oldid=")
            for r in result["results"]
        ):
            raise ValueError("Missing text or revision citation")
        report["queries"].append({"query": query, "titles": titles, "seconds": round(time.monotonic() - start, 3)})
    article = wikipedia_article.invoke({"title": "Казань", "max_chars": 1000})
    if not article["found"] or not article["text"] or not article["images"]:
        raise ValueError("Root text/image lookup failed")
    report.update({"status": "passed", "root_image_links": article["image_count"]})
    (folder / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print("VALIDATION", json.dumps(report, ensure_ascii=False), flush=True)
    return report
