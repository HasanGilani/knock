import json

from knock.cli import main


def test_cli_workflow(make_repo, config, capsys):
    root = make_repo()
    home = ["--home", str(config.home)]
    assert main(home + ["index", str(root)]) == 0
    out = capsys.readouterr().out
    assert "completed" in out and "0 model calls" in out
    assert main(home + ["index", str(root)]) == 0
    assert "0 added, 0 modified" in capsys.readouterr().out

    assert main(home + ["search", "deploy production", "--repo", str(root), "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["results"][0]["category"] == "workflow"

    assert main(home + ["context", "change discount rules", "--repo", str(root)]) == 0
    assert "src/orders/pricing.py" in capsys.readouterr().out

    assert main(home + ["record", "--repo", str(root), "--category", "workflow", "--content",
                        "Release tags must match v<major>.<minor>.<patch> or the deploy workflow is skipped.",
                        "--source", ".github/workflows/deploy.yml:1-4"]) == 0
    rec = json.loads(capsys.readouterr().out)
    assert main(home + ["show", rec["id"], "--repo", str(root)]) == 0
    assert ".github/workflows/deploy.yml" in capsys.readouterr().out

    assert main(home + ["source", "src/orders/pricing.py", "--lines", "1-3", "--repo", str(root)]) == 0
    assert capsys.readouterr().out.startswith("1\t")

    assert main(home + ["status", "--repo", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["freshness"]["state"] == "fresh"
    assert main(home + ["metrics", "--repo", str(root)]) == 0
    assert "measured" in json.loads(capsys.readouterr().out)

    assert main(home + ["source", "../outside", "--repo", str(root)]) == 2
    assert "error" in capsys.readouterr().err

    assert main(home + ["forget", str(root)]) == 1
    capsys.readouterr()
    assert main(home + ["forget", str(root), "--yes"]) == 0
    capsys.readouterr()
    assert main(home + ["repos"]) == 0
    assert json.loads(capsys.readouterr().out) == []
