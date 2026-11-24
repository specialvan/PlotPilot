from scripts.analyze_architecture import baseline_additions


def test_baseline_rejects_new_import_even_when_an_old_one_is_removed():
    baseline = {
        "source_layer": "application", "target_layer": "infrastructure",
        "imports": [{"path": "application/a.py", "statement": "import infrastructure.old", "count": 2}],
    }
    result = {"imports": [{
        "path": "application/a.py", "statement": "import infrastructure.new",
        "source_layer": "application", "target_layer": "infrastructure",
    }]}
    assert baseline_additions(result, baseline) == [
        {"path": "application/a.py", "statement": "import infrastructure.new", "count": 1},
    ]
    result["imports"][0]["statement"] = "import infrastructure.old"
    assert baseline_additions(result, baseline) == []
    result["imports"] *= 3
    assert baseline_additions(result, baseline)[0]["count"] == 1
