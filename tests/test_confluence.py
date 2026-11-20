"""Confluence en lecture seule (ticket « Intégration de JIRA »)."""

import asyncio

from worker.activities import jira_text, jira_tools


def test_requete_nettoyee():
    q = jira_tools.confluence_query('[Test ADLC - vrai bug] Faute « reopenned » dans l\'historique "devis"')
    assert q == "Faute reopenned dans l historique devis"
    assert '"' not in q and "[" not in q


def test_pages_format_v1_cql():
    res = {"_links": {"base": "https://toollive.atlassian.net/wiki"},
           "results": [{"content": {"title": "Statuts OEM – guide prestataire",
                                    "_links": {"webui": "/spaces/TL/pages/1"}},
                        "excerpt": "Historique du @@@hl@@@devis@@@endhl@@@"}]}
    pages = jira_tools._confluence_pages(res, 3)
    assert pages == [{"title": "Statuts OEM – guide prestataire",
                      "url": "https://toollive.atlassian.net/wiki/spaces/TL/pages/1",
                      "excerpt": "Historique du devis"}]


def test_pages_format_liste_et_limite():
    res = [{"title": f"Page {i}", "url": f"https://x/{i}"} for i in range(5)]
    assert [p["title"] for p in jira_tools._confluence_pages(res, 3)] == ["Page 0", "Page 1", "Page 2"]
    assert jira_tools._confluence_pages("texte brut", 3) == []


class FakeClient:
    def __init__(self, fail_first):
        self.fail_first, self.calls = fail_first, []

    async def call_tool(self, tool_name, arguments):
        self.calls.append(tool_name)
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("Unknown tool")
        return {"content": [{"type": "text",
                             "text": '{"results": [{"title": "Doc devis", "url": "https://x/1"}]}'}]}


def test_repli_sur_le_second_outil(monkeypatch):
    monkeypatch.setattr(jira_tools, "cloud_id", lambda: "cid")
    client = FakeClient(fail_first=True)
    res = asyncio.run(jira_tools._confluence_search(client, "Faute reopenned", 3))
    assert res["ok"] and res["pages"][0]["title"] == "Doc devis"
    assert client.calls == ["searchConfluence", "searchConfluenceUsingCql"]


def test_jamais_bloquant(monkeypatch):
    monkeypatch.setattr(jira_tools, "cloud_id", lambda: "cid")

    class Broken:
        async def call_tool(self, tool_name, arguments):
            raise RuntimeError("403 Insufficient scopes")

    res = asyncio.run(jira_tools._confluence_search(Broken(), "Faute", 3))
    assert res["ok"] is False and res["pages"] == []


def test_note_doc_aval():
    assert jira_text.doc_to_check_text([]) == ""
    t = jira_text.doc_to_check_text([{"title": "Guide devis", "url": "https://x/1"}])
    assert "📚" in t and "[Guide devis](https://x/1)" in t


def test_cql_toujours_envoye(monkeypatch):
    """Constat 65fe3c32 : searchConfluence (v2) exige l'argument `cql`."""
    monkeypatch.setattr(jira_tools, "cloud_id", lambda: "cid")
    seen = []

    class Client:
        async def call_tool(self, tool_name, arguments):
            seen.append((tool_name, arguments))
            return {"content": [{"type": "text", "text": '{"results": []}'}]}

    res = asyncio.run(jira_tools._confluence_search(Client(), "Faute reopenned", 3))
    assert res == {"ok": True, "pages": []}
    tool, args = seen[0]
    assert tool == "searchConfluence"
    assert args["cql"] == 'type = page AND text ~ "Faute reopenned"'
