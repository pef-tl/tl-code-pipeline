"""Priorité du PAT : `pipeline_git_token` (propre au déploiement) avant
`gitlab_token` (secret partagé du workspace)."""

from worker.activities import git_tools


def _clear(monkeypatch):
    for name in ("gitlab_token", "GITLAB_TOKEN", "pipeline_git_token", "PIPELINE_GIT_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_pipeline_git_token_prioritaire(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("gitlab_token", "partage")
    monkeypatch.setenv("pipeline_git_token", "perso\n")
    assert git_tools._token_from_env() == ("perso", "pipeline_git_token")


def test_gitlab_token_seul(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GITLAB_TOKEN", "partage")
    assert git_tools._token_from_env() == ("partage", "GITLAB_TOKEN")


def test_aucun_token(monkeypatch):
    _clear(monkeypatch)
    assert git_tools._token_from_env() == (None, None)
