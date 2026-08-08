# MCP File Transfer

EDR-WD exposes chunked file transfer between an MCP client and a target. The
tools intentionally expose only a dedicated sandbox, not the target's general
filesystem.

## Sandbox

The target stores transferred files under:

```text
EDR_WD_TRANSFER_DIR
```

If unset, the default is:

```text
~/Desktop/edr-wd-record/transfers
```

Every tool accepts a relative path inside this directory. Absolute paths,
drive-qualified paths, `..` traversal, and symbolic-link escapes are rejected.
One decoded chunk is limited to 1 MiB and one file to 100 MiB. No MCP delete
tool is provided.

## Upload

Call `transfer_upload` with base64 content. The first chunk uses `offset=0`.
Existing files are rejected unless `overwrite=true` is explicit. Later chunks
must use the exact `next_offset` returned by the previous call.

Chunks are written to a private staging file. The destination is created or
replaced atomically only after the final chunk passes SHA-256 validation, so an
interrupted or invalid upload does not publish a partial file.

On the final chunk, set `final=true` and preferably pass the complete file's
SHA-256 as `expected_sha256`:

```json
{
  "relative_path": "cases/input.zip",
  "content_base64": "...",
  "offset": 262144,
  "final": true,
  "expected_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

## Download

Call `transfer_download` repeatedly with the returned `next_offset` until
`eof=true`:

```json
{
  "relative_path": "results/output.zip",
  "offset": 0,
  "max_bytes": 262144
}
```

Each response contains `content_base64`, `bytes_read`, `next_offset`, and
`eof`. The final response also contains the full file SHA-256.

## Metadata And Resume

`transfer_stat(relative_path)` returns the current size, SHA-256, modification
timestamp, and `complete` state. When an upload is staged, clients can use its
size to resume, but the next upload offset must exactly equal the staged size.

These tools are for explicit user-requested data exchange. They do not change
the normal lifecycle contract: connect, start, stop, test, and E2E operations
must not silently upload files.

## Agent CLI And SCP Fallback

The agent CLI uses MCP first and automatically falls back to the repository's
Paramiko SFTP implementation when the MCP service or transfer tools are
unavailable:

```bash
edr-wd --target win-dev file-upload ./input.zip cases/input.zip
edr-wd --target win-dev file-download results/output.zip ./output.zip
```

The JSON result always reports `transport: "mcp"` or `transport: "scp"`.
`--no-scp-fallback` disables fallback. Application and safety failures such as
`file_exists`, invalid paths, or checksum mismatches never fall back, because
doing so would bypass the MCP transfer contract.

SCP fallback is implemented with Paramiko SFTP, consistent with the rest of
EDR-WD. It writes under the same transfer root. Set
`file_transfer.remote_root` in the target config when the target-side
`EDR_WD_TRANSFER_DIR` differs from the default location.
