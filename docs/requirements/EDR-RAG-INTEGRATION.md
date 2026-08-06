# edr-rag Integration Boundary

`edr-rag` is intentionally maintained as a separate package/repository from
`edr-wd`.

Responsibility split:

```text
edr-rag:
  CHM manual ingestion
  manual/procedure/action search
  action catalog generation
  feedback collection

edr-wd:
  target lifecycle
  GUI automation
  PowerShell execution
  action execution evidence
```

The two projects should integrate through MCP or a small client adapter, not by
importing each other's internal modules.

Expected runtime layout:

```text
edr-rag MCP:
  action_search
  procedure_search
  get_action
  submit_feedback

edr-wd MCP:
  connect
  dump_tree
  click_target
  run_powershell
```

Agent workflow:

```text
1. Use edr-rag to decide what to do.
2. Check risk and confirmation policy from edr-rag.
3. Use edr-wd to execute GUI/PowerShell steps.
4. Send failures or corrections back to edr-rag feedback.
```

Design reference:

```text
edr-rag/docs/requirements/EDR-RAG-DESIGN.md
```
