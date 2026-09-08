# Publish Agent Output

**Need:** Make an agent's generated artifact available in another working
context, whether the next consumer is the same person, another agent, or a
tool. An agent is not restricted to MCP: CLI, filesystem, Web, and API access
are alternatives when its environment provides them.

**Prerequisites:** For this MCP example, a trusted host launches the Pasteberth
stdio adapter, can reach the daemon, and supplies authentication through
`PASTEBERTH_PASSWORD` when needed. The zone ID `alpha-work-exchange` is already
known and active, and `analysis-summary.txt` is an unused target name.

## Steps

1. Configure the adapter invocation using [integrations](../integrations.md). Tell the caller the exact zone ID; MCP does not list or discover Pasteberth zones.
2. Finish the analysis, then call the MCP `drop` tool with these arguments:

```json
{
  "zone": "alpha-work-exchange",
  "items": [
    {
      "filename": "analysis-summary.txt",
      "content": "Totals checked against the source workbook. Review the exclusions before reuse.\n"
    }
  ]
}
```

3. Inspect `isError` and the tool result's JSON text, including `items` and `errors`. Return only references from successful publications.
4. Retrieve the result through an available interface. A browser can download or copy supported text; a tool can read the returned path only if it shares that server-side filesystem namespace.

## Observable Result

The summary is a managed text file with a history entry and applicable copy and
download actions. It can be reused without Pasteberth assigning sender or
receiver identities.

## Pitfalls

- MCP exposes only `drop`, not retrieval, comments, transfers, or filesystem registration. Use the documented API or another available interface for those operations.
- For a generated local file, use an item with `path` instead of supplied `content` and `filename`. The path is local to the adapter process, not automatically to the daemon or other tools.
- A multi-item call may partly succeed. A normal MCP process exit is not proof that every tool publication succeeded.
- Do not publish secrets merely because the adapter can read them. Its filesystem access follows its account, and groups in the UI are not ACLs.

See [MCP reference](../reference/mcp.md), [script output](script-output.md),
[documents](documents.md), and [current result](current-result.md).
