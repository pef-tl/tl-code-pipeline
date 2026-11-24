"""Tests ciblés de l'intégration JIRA (pas de Temporal, pas de réseau) :
- helpers purs de worker.activities.jira_tools (cloud_id, ADF, parsing, appels
  tool avec un ToolCallClient stub) ;
- le workflow spec-to-tests déclare bien le slot connecteur `atlassian`
  (@uses_connectors) et son entrypoint accepte `jira_issue`.

Run : uv run --with pytest python -m pytest tests/ -q
"""

import asyncio
import inspect
import json

import pytest

import worker.activities.jira_tools as jt


class StubClient:
    """ToolCallClient stub : réponses prédéfinies par tool, appels enregistrés."""

    def __init__(self, responses, fail_on=(), errors=None):
        self.responses = responses
        self.fail_on = set(fail_on)
        self.errors = errors or {}
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, tool_name, arguments=None):
        self.calls.append((tool_name, arguments or {}))
        if tool_name in self.errors:
            raise self.errors[tool_name]
        if tool_name in self.fail_on:
            raise RuntimeError(f"boom {tool_name}")
        payload = self.responses[tool_name]
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return {"content": [{"type": "text", "text": text}]}


ISSUE = {
    "id": "62978",
    "key": "TOOLLIVE-3600",
    "fields": {
        "summary": "Récupérer les infos de la CRTL",
        "description": "Lire les colonnes R, S, T du fichier CRTL.\n",
        "status": {"name": "new"},
        "issuetype": {"name": "Task"},
        "priority": {"name": "Medium"},
        "labels": ["c8y"],
        "assignee": None,
        "reporter": {"displayName": "pef"},
        "comment": {"comments": [
            {"author": {"displayName": "pef"},
             "body": {"type": "doc", "content": [
                 {"type": "paragraph", "content": [{"type": "text", "text": "Hello "},
                                                   {"type": "text", "text": "world"}]}]}},
        ]},
    },
}

TRANSITIONS = {"transitions": [
    {"id": "21", "name": "In Progress", "to": {"name": "devs-en-cours"}, "isAvailable": True},
    {"id": "61", "name": "code-review", "to": {"name": "code-review"}, "isAvailable": True},
]}


def test_cloud_id_default(monkeypatch):
    monkeypatch.delenv("jira_cloud_id", raising=False)
    monkeypatch.delenv("JIRA_CLOUD_ID", raising=False)
    monkeypatch.delenv("jira_base_url", raising=False)
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    assert jt.cloud_id() == "toollive.atlassian.net"


def test_cloud_id_from_env(monkeypatch):
    monkeypatch.setenv("jira_cloud_id", "9aa8afda-b2d8-45cc-bd9c-ea65a7606cb8")
    assert jt.cloud_id() == "9aa8afda-b2d8-45cc-bd9c-ea65a7606cb8"
    monkeypatch.delenv("jira_cloud_id")
    monkeypatch.setenv("jira_base_url", "https://toollive.atlassian.net/jira")
    assert jt.cloud_id() == "toollive.atlassian.net"


def test_issue_url(monkeypatch):
    monkeypatch.delenv("jira_base_url", raising=False)
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    assert jt.issue_url("TOOLLIVE-3600") == "https://toollive.atlassian.net/jira/browse/TOOLLIVE-3600"


def test_adf_to_text():
    adf = {"type": "doc", "content": [
        {"type": "heading", "content": [{"type": "text", "text": "Titre"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "ligne"}]},
    ]}
    assert jt._adf_to_text(adf) == "Titre\nligne"
    assert jt._adf_to_text(None) == ""
    assert jt._adf_to_text("plain") == "plain"


def test_parse_tool_result():
    dumped = {"content": [{"type": "text", "text": json.dumps({"a": 1})}]}
    assert jt._parse_tool_result(dumped) == {"a": 1}
    dumped = {"content": [{"type": "text", "text": "pas du json"}]}
    assert jt._parse_tool_result(dumped) == "pas du json"


def test_fetch_issue_normalizes():
    stub = StubClient({"getJiraIssue": ISSUE})
    out = asyncio.run(jt._fetch_issue(stub, "TOOLLIVE-3600"))
    tool, args = stub.calls[0]
    assert tool == "getJiraIssue"
    assert args["issueIdOrKey"] == "TOOLLIVE-3600"
    assert args["cloudId"] == "toollive.atlassian.net"
    assert "comment" in args["fields"]
    assert out["key"] == "TOOLLIVE-3600"
    assert out["summary"] == "Récupérer les infos de la CRTL"
    assert out["status"] == "new"
    assert out["labels"] == ["c8y"]
    assert out["url"].endswith("/browse/TOOLLIVE-3600")
    assert out["comments"][0]["body"] == "Hello world"


def test_fetch_issue_not_found_raises():
    stub = StubClient({"getJiraIssue": {"errorMessages": ["Issue does not exist"]}})
    with pytest.raises(RuntimeError, match="introuvable"):
        asyncio.run(jt._fetch_issue(stub, "NOPE-1"))


def test_fetch_issue_unwraps_v2_data_envelope():
    """Cas prod : exécution 0138f8a4...a2a9c / run 01a1effa-ca7a-773e-ada1-335119a7aae7
    (2026-11-18T18:45Z) — le connecteur atlassian_49bb (serveur MCP v2) enveloppe le
    ticket dans {"data": {...}} : getJiraIssue renvoie bien TOOLLIVE-3601 (id 62981)
    mais « key » n'est pas au premier niveau ; l'ancien code levait un faux
    « introuvable ou illisible » et tuait le run."""
    v2 = {"data": {"appliedContentFormat": "markdown", "id": "62981", "key": "TOOLLIVE-3601",
                   "fields": {"summary": "[Test ADLC] Le nombre d'assets n'est pas bon",
                              "description": "Remplacer «50000 connected assets» par «90000+ connected assets»",
                              "status": {"name": "new", "statusCategory": {"name": "To Do"}},
                              "assignee": None, "priority": {"name": "Medium"}, "labels": []}}}
    stub = StubClient({"getJiraIssue": v2})
    out = asyncio.run(jt._fetch_issue(stub, "TOOLLIVE-3601"))
    assert out["key"] == "TOOLLIVE-3601"
    assert out["id"] == "62981"
    assert out["summary"] == "[Test ADLC] Le nombre d'assets n'est pas bon"
    assert out["status"] == "new"
    assert out["url"].endswith("/browse/TOOLLIVE-3601")


def test_fetch_issue_wrapped_error_still_fatal():
    stub = StubClient({"getJiraIssue": {"data": {"errorMessages": ["Issue does not exist"]}}})
    with pytest.raises(RuntimeError, match="introuvable"):
        asyncio.run(jt._fetch_issue(stub, "NOPE-1"))


def test_search_unwraps_v2_data_envelope():
    stub = StubClient({"searchJiraIssuesUsingJql": {"data": {"issues": [ISSUE]}}})
    out = asyncio.run(jt._search(stub, "key = TOOLLIVE-3600", 5))
    assert out[0]["key"] == "TOOLLIVE-3600" and out[0]["status"] == "new"


def test_transition_unwraps_v2_data_envelope():
    stub = StubClient({"getTransitionsForJiraIssue": {"data": TRANSITIONS},
                      "transitionJiraIssue": {}})
    out = asyncio.run(jt._transition(stub, "TOOLLIVE-3600", "code-review"))
    assert out["ok"] is True and out["to"] == "code-review"


class FakeGatewayError(Exception):
    """Simule une SDKError de la gateway connecteurs (status_code + body)."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


def test_fetch_issue_soft_when_connector_unavailable_401():
    stub = StubClient({}, errors={"getJiraIssue": FakeGatewayError(
        'API error occurred: Status 401. Body: {"detail":"Authentication failed against '
        'MCP server while calling tool"}', status_code=401)})
    out = asyncio.run(jt._fetch_issue_or_unavailable(stub, "TOOLLIVE-3600"))
    assert out["ok"] is False
    assert out["reason"] == "connector_unavailable"
    assert "401" in out["error"]


def test_fetch_issue_soft_on_gateway_hint_without_status():
    stub = StubClient({}, errors={"getJiraIssue": FakeGatewayError(
        "The connector has no connection this caller can use")})
    out = asyncio.run(jt._fetch_issue_or_unavailable(stub, "TOOLLIVE-3600"))
    assert out["ok"] is False and out["reason"] == "connector_unavailable"


def test_fetch_issue_or_unavailable_still_raises_on_bad_key():
    stub = StubClient({"getJiraIssue": {"errorMessages": ["Issue does not exist"]}})
    with pytest.raises(RuntimeError, match="introuvable"):
        asyncio.run(jt._fetch_issue_or_unavailable(stub, "NOPE-1"))


def test_fetch_issue_or_unavailable_raises_on_other_errors():
    stub = StubClient({}, errors={"getJiraIssue": FakeGatewayError(
        "API error occurred: Status 500", status_code=500)})
    with pytest.raises(FakeGatewayError):
        asyncio.run(jt._fetch_issue_or_unavailable(stub, "TOOLLIVE-3600"))


class FakeMcpToolError(Exception):
    """Simule ConnectorToolCallError : str(exc) générique, réponse MCP dans `details`
    (invisible dans str(exc), comme en prod quand le tool renvoie une erreur MCP
    malgré un HTTP 200 de la gateway)."""

    def __init__(self, response):
        super().__init__("[01a1ef7f-0488-7652-934c-83b2eca95331] connector tool call failed")
        self.details = (response,)


def _mcp_error_response(message: str, status_code: int | None = None):
    payload = {"error": True, "message": message}
    if status_code is not None:
        payload["statusCode"] = status_code
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def test_fetch_issue_soft_on_mcp_api_token_permission_error():
    """Cas prod : exécution 0138f8a4...767f50 / run 01a1ef65-f20c-728a-957f-da137a617e40
    (2026-11-18T16:03Z) — la gateway répond 200 mais le tool getJiraIssue renvoie
    « You don't have permission to connect via API token. Please ask your organization
    admin for access. » dans son contenu : le run doit continuer sans le ticket."""
    stub = StubClient({}, errors={"getJiraIssue": FakeMcpToolError(_mcp_error_response(
        "You don't have permission to connect via API token. "
        "Please ask your organization admin for access."))})
    out = asyncio.run(jt._fetch_issue_or_unavailable(stub, "TOOLLIVE-3601"))
    assert out["ok"] is False
    assert out["reason"] == "connector_unavailable"
    assert "api token" in out["error"].lower()


def test_fetch_issue_soft_on_mcp_insufficient_scopes_error():
    """Cas prod : exécution 0138f8a4...c2740 / run 01a1ef8e-f451-75d8-b273-e66aae99cec2
    (2026-11-18T16:48Z) — la gateway répond 200 mais le tool getJiraIssue du connecteur
    atlassian_49bb (01a1ef7f-0488-7652-934c-83b2eca95331) renvoie dans son contenu
    403 « Insufficient scopes for "getJiraIssue". Required: [read:jira:agent-interface].
    Use discover to find endpoints you have access to. » : le run doit continuer sans
    le ticket (JIRA ne tue jamais le pipeline)."""
    stub = StubClient({}, errors={"getJiraIssue": FakeMcpToolError(_mcp_error_response(
        'Insufficient scopes for "getJiraIssue". Required: [read:jira:agent-interface]. '
        "Use discover to find endpoints you have access to.", status_code=403))})
    out = asyncio.run(jt._fetch_issue_or_unavailable(stub, "TOOLLIVE-3602"))
    assert out["ok"] is False
    assert out["reason"] == "connector_unavailable"
    assert "insufficient scopes" in out["error"].lower()


def test_fetch_issue_still_fatal_on_issue_level_403_mcp_error():
    """Une 403 au niveau du ticket (et non des scopes du connecteur) reste fatale :
    seul « insufficient scope » est traité comme connecteur indisponible."""
    stub = StubClient({}, errors={"getJiraIssue": FakeMcpToolError(_mcp_error_response(
        "Issue does not exist or you do not have permission to see it.", status_code=403))})
    with pytest.raises(FakeMcpToolError):
        asyncio.run(jt._fetch_issue_or_unavailable(stub, "NOPE-1"))


def test_fetch_issue_still_fatal_on_issue_level_mcp_error():
    """Erreur MCP au niveau du ticket (mauvaise clé) : ce n'est PAS un connecteur
    indisponible, l'exception doit remonter (ticket introuvable = fatal)."""
    stub = StubClient({}, errors={"getJiraIssue": FakeMcpToolError(_mcp_error_response(
        "Issue does not exist or you do not have permission to see it."))})
    with pytest.raises(FakeMcpToolError):
        asyncio.run(jt._fetch_issue_or_unavailable(stub, "NOPE-1"))


def test_comment_ok_and_non_blocking_error():
    stub = StubClient({"addCommentToJiraIssue": {"id": "1"}})
    out = asyncio.run(jt._comment(stub, "TOOLLIVE-3600", "hello"))
    assert out == {"ok": True, "id": "1"}
    _, args = stub.calls[0]
    assert args["commentBody"] == "hello" and args["contentFormat"] == "markdown"
    failing = StubClient({}, fail_on=("addCommentToJiraIssue",))
    out = asyncio.run(jt._comment(failing, "TOOLLIVE-3600", "hello"))
    assert out["ok"] is False and "boom" in out["error"]


def test_transition_by_name():
    """Serveur v2 en premier : listJiraIssueTransitions puis transitionJiraIssue(transitionId)."""
    stub = StubClient({"listJiraIssueTransitions": TRANSITIONS, "transitionJiraIssue": {}})
    out = asyncio.run(jt._transition(stub, "TOOLLIVE-3600", "code-review"))
    assert out["ok"] is True and out["to"] == "code-review"
    tool, args = stub.calls[1]
    assert tool == "transitionJiraIssue" and args["transitionId"] == "61"


def test_transition_v1_si_v2_absent():
    stub = StubClient({"getTransitionsForJiraIssue": TRANSITIONS, "transitionJiraIssue": {}},
                      errors={"listJiraIssueTransitions": RuntimeError("Unknown tool")})
    out = asyncio.run(jt._transition(stub, "TOOLLIVE-3600", "code-review"))
    assert out["ok"] is True
    assert stub.calls[0][0] == "listJiraIssueTransitions"
    assert "getTransitionsForJiraIssue" in [c[0] for c in stub.calls]


def test_transition_nom_souple_et_statut_toollive():
    """« devs-en-cours » trouvé par le statut d'arrivée ; « Devs en cours » aussi."""
    stub = StubClient({"listJiraIssueTransitions": TRANSITIONS, "transitionJiraIssue": {}})
    out = asyncio.run(jt._transition(stub, "TOOLLIVE-3611", "Devs en cours"))
    assert out["ok"] is True and out["to"] == "devs-en-cours"


def test_transition_indisponible_liste_les_statuts():
    stub = StubClient({"listJiraIssueTransitions": TRANSITIONS})
    out = asyncio.run(jt._transition(stub, "TOOLLIVE-3611", "Done"))
    assert out["ok"] is False
    assert "disponibles : code-review, devs-en-cours" in out["reason"]


def test_transition_erreur_complete_conservee():
    """Constat 3601-3611 : « connector tool call failed » sans la raison."""
    class Err(Exception):
        details = [{"content": [{"type": "text", "text": "MCP error -32602: Required at transitionId"}]}]

    stub = StubClient({"listJiraIssueTransitions": TRANSITIONS},
                      errors={"transitionJiraIssue": Err("connector tool call failed")})
    out = asyncio.run(jt._transition(stub, "TOOLLIVE-3611", "code-review"))
    assert out["ok"] is False and "Required at transitionId" in out["error"]


def test_statut_de_demarrage_par_defaut(monkeypatch):
    monkeypatch.delenv("jira_transition_start", raising=False)
    monkeypatch.delenv("JIRA_TRANSITION_START", raising=False)
    assert jt.start_transition_name() == "devs-en-cours"
    monkeypatch.setenv("jira_transition_start", "Pris en charge")
    assert jt.start_transition_name() == "Pris en charge"


def test_call_falls_back_to_v2_tool_name():
    stub = StubClient({"listJiraIssueTransitions": TRANSITIONS},
                      errors={"getTransitionsForJiraIssue": FakeGatewayError(
                          'Status 404. Body: {"detail":"Tool getTransitionsForJiraIssue not found"}',
                          status_code=404)})
    out = asyncio.run(jt._call(stub, "getTransitionsForJiraIssue",
                               cloudId="toollive.atlassian.net", issueIdOrKey="TOOLLIVE-3600"))
    assert out == TRANSITIONS
    assert [c[0] for c in stub.calls] == ["getTransitionsForJiraIssue", "listJiraIssueTransitions"]


def test_call_reraises_when_tool_has_no_fallback():
    stub = StubClient({}, errors={"getJiraIssue": FakeGatewayError(
        'Status 404. Body: {"detail":"Tool getJiraIssue not found"}', status_code=404)})
    with pytest.raises(FakeGatewayError):
        asyncio.run(jt._call(stub, "getJiraIssue", cloudId="x", issueIdOrKey="NOPE-1"))
    assert [c[0] for c in stub.calls] == ["getJiraIssue"]


def test_call_falls_back_on_mcp_unknown_tool_content_error():
    """Cas prod : exécution 0138f8a4...b2363 / run 01a1f00d-42f0-7593-8576-dbf2d3a55e33
    (2026-11-18T19:06:23Z) — la gateway répond 200 mais le serveur MCP v2 renvoie
    « Unknown tool: 'getTransitionsForJiraIssue' » DANS le contenu du tool
    (ConnectorToolCallError, texte dans `details`) : le fallback v2 doit se
    déclencher (listJiraIssueTransitions)."""
    unknown = FakeMcpToolError({"content": [
        {"type": "text", "text": "Unknown tool: 'getTransitionsForJiraIssue'"}]})
    stub = StubClient({"listJiraIssueTransitions": TRANSITIONS},
                      errors={"getTransitionsForJiraIssue": unknown})
    out = asyncio.run(jt._call(stub, "getTransitionsForJiraIssue",
                               cloudId="toollive.atlassian.net", issueIdOrKey="TOOLLIVE-3601"))
    assert out == TRANSITIONS
    assert [c[0] for c in stub.calls] == ["getTransitionsForJiraIssue", "listJiraIssueTransitions"]


def test_comment_falls_back_on_mcp_unknown_tool_content_error():
    """Idem pour addCommentToJiraIssue -> addOrEditJiraIssueComment (même exécution,
    19:06:25Z) : le commentaire doit être posté via le nom v2."""
    unknown = FakeMcpToolError({"content": [
        {"type": "text", "text": "Unknown tool: 'addCommentToJiraIssue'"}]})
    stub = StubClient({"addOrEditJiraIssueComment": {"id": "1"}},
                      errors={"addCommentToJiraIssue": unknown})
    out = asyncio.run(jt._comment(stub, "TOOLLIVE-3601", "hello"))
    assert out == {"ok": True, "id": "1"}
    assert [c[0] for c in stub.calls] == ["addCommentToJiraIssue", "addOrEditJiraIssueComment"]


def test_call_no_fallback_on_other_mcp_content_error():
    """Une erreur MCP dans le contenu qui n'est PAS « unknown tool » (ex. erreur au
    niveau du ticket) ne doit pas déclencher le fallback vers le nom v2."""
    err = FakeMcpToolError({"content": [
        {"type": "text", "text": "Issue does not exist or you do not have permission to see it."}]})
    stub = StubClient({"listJiraIssueTransitions": TRANSITIONS},
                      errors={"getTransitionsForJiraIssue": err})
    with pytest.raises(FakeMcpToolError):
        asyncio.run(jt._call(stub, "getTransitionsForJiraIssue",
                             cloudId="x", issueIdOrKey="NOPE-1"))
    assert [c[0] for c in stub.calls] == ["getTransitionsForJiraIssue"]


def test_transition_accepts_bare_list_response():
    stub = StubClient({"getTransitionsForJiraIssue": TRANSITIONS["transitions"],
                       "transitionJiraIssue": {}})
    out = asyncio.run(jt._transition(stub, "TOOLLIVE-3600", "In Progress"))
    assert out["ok"] is True and out["to"] == "devs-en-cours"


def test_transition_falls_back_to_v2_transition_id():
    class V2Stub(StubClient):
        def __init__(self):
            super().__init__({})
            self.transition_args = []

        async def call_tool(self, tool_name, arguments=None):
            self.calls.append((tool_name, arguments or {}))
            if tool_name in ("getTransitionsForJiraIssue", "listJiraIssueTransitions"):
                return {"content": [{"type": "text", "text": json.dumps(TRANSITIONS)}]}
            if tool_name == "transitionJiraIssue":
                self.transition_args.append(arguments or {})
                if "transition" in (arguments or {}):
                    raise FakeGatewayError("Status 400 invalid transition payload", status_code=400)
                return {"content": [{"type": "text", "text": "{}"}]}
            raise AssertionError(f"unexpected tool {tool_name}")

    stub = V2Stub()
    out = asyncio.run(jt._transition(stub, "TOOLLIVE-3600", "code-review"))
    assert out["ok"] is True and out["to"] == "code-review"
    assert [c[0] for c in stub.calls] == ["listJiraIssueTransitions", "transitionJiraIssue"]
    assert stub.transition_args[-1]["transitionId"] == "61"


def test_transition_unavailable_is_soft():
    stub = StubClient({"getTransitionsForJiraIssue": TRANSITIONS})
    out = asyncio.run(jt._transition(stub, "TOOLLIVE-3600", "Done"))
    assert out["ok"] is False and "indisponible" in out["reason"]
    out = asyncio.run(jt._transition(stub, "TOOLLIVE-3600", ""))
    assert out["ok"] is False


def test_search_briefs():
    stub = StubClient({"searchJiraIssuesUsingJql": {"issues": [ISSUE]}})
    out = asyncio.run(jt._search(stub, "key = TOOLLIVE-3600", 5))
    _, args = stub.calls[0]
    assert args["jql"] == "key = TOOLLIVE-3600" and args["maxResults"] == 5
    assert out[0]["key"] == "TOOLLIVE-3600" and out[0]["status"] == "new"


def test_workflow_declares_atlassian_slot():
    import worker.workflows.spec_to_tests as m
    meta = getattr(m.SpecToTests, "__plugin_metadata__", {})
    assert jt.DEFAULT_JIRA_CONNECTOR_NAME in json.dumps(meta)


def test_atlassian_slot_configured_for_managed_runs(monkeypatch):
    for var in ("jira_connector_name", "JIRA_CONNECTOR_NAME",
                "jira_credentials_name", "JIRA_CREDENTIALS_NAME"):
        monkeypatch.delenv(var, raising=False)
    slot = jt._build_atlassian_connector()
    assert slot.connector_name == jt.DEFAULT_JIRA_CONNECTOR_NAME == "01a1ef7f-0488-7652-934c-83b2eca95331"
    assert slot.run_as.value == "deployment"
    assert slot.auto_auth is False
    assert slot.credentials_name is None
    monkeypatch.setenv("jira_connector_name", "atlassian_47af")
    assert jt._build_atlassian_connector().connector_name == "atlassian_47af"
    monkeypatch.setenv("jira_credentials_name", "jira-bot")
    assert jt._build_atlassian_connector().credentials_name == "jira-bot"
    monkeypatch.setenv("JIRA_CREDENTIALS_NAME", "jira-bot-legacy")
    monkeypatch.delenv("jira_credentials_name")
    assert jt._build_atlassian_connector().credentials_name == "jira-bot-legacy"


def test_declared_slot_skips_auto_auth_preflight():
    assert jt.atlassian_connector.run_as.value == "deployment"
    assert jt.atlassian_connector.auto_auth is False


def test_workflow_metadata_disables_connector_preflight():
    import worker.workflows.spec_to_tests as m
    meta = json.dumps(getattr(m.SpecToTests, "__plugin_metadata__", {}))
    assert '"auto_auth": false' in meta
    assert '"run_as": "deployment"' in meta


def test_entrypoint_accepts_jira_issue():
    import worker.workflows.spec_to_tests as m
    fn = m.SpecToTests.run
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    params = inspect.signature(fn).parameters
    assert "jira_issue" in params
    assert params["jira_issue"].default == ""


def test_transitions_via_getjiraissue_expand():
    """Constat 41167b17 : ni listJiraIssueTransitions ni getTransitionsForJiraIssue
    n'existent sur le connecteur v2 -> getJiraIssue(expand=transitions)."""
    unknown = RuntimeError("connector tool call failed\nUnknown tool")
    issue = {"key": "TOOLLIVE-3611", "fields": {"status": {"name": "Nouveau"}},
             "transitions": TRANSITIONS["transitions"]}
    stub = StubClient({"getJiraIssue": issue, "transitionJiraIssue": {}},
                      errors={"listJiraIssueTransitions": unknown, "getTransitionsForJiraIssue": unknown})
    out = asyncio.run(jt._transition(stub, "TOOLLIVE-3611", "devs-en-cours"))
    assert out["ok"] is True and out["to"] == "devs-en-cours"
    get_calls = [a for t, a in stub.calls if t == "getJiraIssue"]
    assert get_calls[0]["expand"] == "transitions"
    assert [a for t, a in stub.calls if t == "transitionJiraIssue"][0]["transitionId"] == "21"


def test_transitions_via_executeread():
    unknown = RuntimeError("Unknown tool")
    stub = StubClient({"getJiraIssue": {"key": "TOOLLIVE-3611", "fields": {}},
                       "executeRead": {"result": TRANSITIONS}, "transitionJiraIssue": {}},
                      errors={"listJiraIssueTransitions": unknown, "getTransitionsForJiraIssue": unknown})
    out = asyncio.run(jt._transition(stub, "TOOLLIVE-3611", "code-review"))
    assert out["ok"] is True
    ex = [a for t, a in stub.calls if t == "executeRead"][0]
    assert ex["name"] == "listJiraIssueTransitions"


def test_transitions_toutes_les_voies_echouent_message_complet():
    unknown = RuntimeError("Unknown tool")
    stub = StubClient({}, errors={t: unknown for t in (
        "listJiraIssueTransitions", "getTransitionsForJiraIssue", "getJiraIssue", "executeRead")})
    out = asyncio.run(jt._transition(stub, "TOOLLIVE-3611", "code-review"))
    assert out["ok"] is False
    for tool in ("listJiraIssueTransitions", "getJiraIssue", "executeRead"):
        assert tool in out["error"]


# ------------------------------------------- commentaire « Suivi » vivant ---

def test_comment_edit_passe_le_comment_id():
    stub = StubClient({"addOrEditJiraIssueComment": {"id": "777"}})
    out = asyncio.run(jt._comment(stub, "TOOLLIVE-3611", "v2", comment_id="777"))
    assert out == {"ok": True, "id": "777"}
    tool, args = stub.calls[0]
    assert tool == "addOrEditJiraIssueComment"
    assert args["commentId"] == "777" and args["commentBody"] == "v2"


def test_comment_id_formes_de_reponse():
    assert jt._comment_id({"id": 12}) == "12"
    assert jt._comment_id({"comment": {"id": "34"}}) == "34"
    assert jt._comment_id({"data": {"id": "56"}}) == "56"
    assert jt._comment_id("Comment 98765 added to TOOLLIVE-3611") == "98765"
    assert jt._comment_id({"ok": True}) == ""


def test_follow_cree_puis_modifie():
    stub = StubClient({"addCommentToJiraIssue": {"id": "500"},
                       "addOrEditJiraIssueComment": {"id": "500"}})
    first = asyncio.run(jt._follow_comment(stub, "TOOLLIVE-3611", "v1"))
    assert first == {"ok": True, "id": "500", "edited": False}
    second = asyncio.run(jt._follow_comment(stub, "TOOLLIVE-3611", "v2", first["id"]))
    assert second == {"ok": True, "id": "500", "edited": True}
    assert [c[0] for c in stub.calls] == ["addCommentToJiraIssue", "addOrEditJiraIssueComment"]


def test_follow_edition_impossible_recree_un_commentaire():
    stub = StubClient({"addCommentToJiraIssue": {"id": "501"}},
                      fail_on=("addOrEditJiraIssueComment",))
    out = asyncio.run(jt._follow_comment(stub, "TOOLLIVE-3611", "v2", "500"))
    assert out["ok"] and out["id"] == "501" and out["edited"] is False
    assert "boom" in out["edit_error"]  # la raison du refus reste visible dans Studio


def test_follow_retrouve_l_id_sur_le_ticket_si_la_reponse_n_en_a_pas():
    """Constat TOOLLIVE-3613 (f05f4b8e) : réponse sans id -> 5 commentaires."""
    issue = {"key": "TOOLLIVE-3613", "fields": {"comment": {"comments": [
        {"id": "10", "body": "Autre commentaire"},
        {"id": "11", "body": "🤖 **Pipeline sur TOOLLIVE-3613** Exécution `aaaaaaaa`"},
        {"id": "12", "body": "🤖 **Pipeline sur TOOLLIVE-3613** Exécution `f05f4b8e`"},
    ]}}}
    stub = StubClient({"addCommentToJiraIssue": "Commentaire ajouté",
                       "getJiraIssue": issue})
    out = asyncio.run(jt._follow_comment(stub, "TOOLLIVE-3613", "v1", "", "f05f4b8e"))
    assert out["ok"] and out["id"] == "12"
    _, args = stub.calls[-1]
    assert args["fields"] == ["comment"]


def test_follow_id_introuvable_reste_vide():
    stub = StubClient({"addCommentToJiraIssue": "ok", "getJiraIssue": {"key": "K-1", "fields": {}}})
    out = asyncio.run(jt._follow_comment(stub, "K-1", "v1", "", "f05f4b8e"))
    assert out["ok"] and out["id"] == ""


def test_entrypoint_request_is_optional():
    import worker.workflows.spec_to_tests as m
    fn = m.SpecToTests.run
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    params = inspect.signature(fn).parameters
    assert "request" in params
    assert params["request"].default == ""
    # `repo` reste la seule entrée obligatoire
    required = [n for n, p in params.items()
                if n != "self" and p.default is inspect.Parameter.empty]
    assert required == ["repo"]
