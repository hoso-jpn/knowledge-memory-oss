# Local stdio MCP

Install `python -m pip install -e '.[mcp]'`, then build the index with `km index`.
The pinned official SDK is `mcp==1.26.0` (v1 API, not an assertion it is the latest release).
Sources: https://github.com/modelcontextprotocol/python-sdk/tree/v1.26.0 and
https://github.com/modelcontextprotocol/python-sdk/blob/v1.26.0/examples/snippets/clients/stdio_client.py

Example local client's configuration (replace all paths; no credentials):

```json
{"mcpServers":{"memory":{"command":"/absolute/venv/bin/km-mcp","args":["--vault","/absolute/vault","--db","/absolute/memory.sqlite3","--scope","personal"]}}}
```

Only search_memory, get_memory and build_context are exposed. Candidate access is an
operator-only startup flag, `--include-candidates`. History is disabled. No model-selected
scope, paths, output files, executable or shell is accepted. Each call rechecks the index's
vault and freshness. Source content is untrusted data; the server does not execute it or
fetch source URLs. Input size, result count, context budget and traversal are bounded.

The client owns the child process: exit/disconnect to close it; no systemd service or
background scheduler is installed. stdout is JSON-RPC only; logging is on stderr.
Installation downloads dependencies. Once installed, the server needs no external service.
This local connection does not connect ChatGPT's cloud UI automatically.

## Verification

`python -m unittest discover -s tests -p test_mcp.py -v` runs a real official SDK stdio
client, initialize, tools/list, search → get, context, forbidden-argument checks and shutdown.
The baseline suite tests state/scope/provenance. CI installs the extra so the protocol test
cannot skip there. Install `.[mcp]` locally as well; without the SDK these tests cannot run.

## Checking that an agent actually uses the server

SDK tool calls do not prove an LLM elected to call tools, so check that separately with the
client you intend to use. In a fresh session, ask a question that needs a stored record (for
example a past decision with reasons and rejected alternatives, next to a distractor record)
and confirm from the trace that the model chose its own search terms and its own
search → get order, cited the record, and reported status, claim kind and timestamps.
A passing check describes that client, version and model only.

With Claude Code, register nothing to run this check. Pass the server per invocation instead,
so no existing client registration is written or restored:

```
env -u NOTION_TOKEN claude -p "<question>" --mcp-config <file>.json --strict-mcp-config \
  --allowed-tools mcp__<name>__search_memory mcp__<name>__get_memory mcp__<name>__build_context \
  --disallowed-tools Bash Read Write WebFetch WebSearch Task ... \
  --output-format stream-json --verbose > trace.jsonl
```

`--strict-mcp-config` keeps that run to the servers in `--mcp-config`; `env -u` keeps a token
out of the child process; `--disallowed-tools` stops file reads or web search standing in for
the server. Read the `tool_use` blocks in the trace to see what the model actually chose, and
verify the version, startup directory and the `CLAUDE.md` → `@AGENTS.md` import in the same
run. Confirm no server process survives the session. Keep traces, answers and note text
outside the repository; never attach private note text to a PR.

The tests refuse an extra argument, a traversal identifier, a substituted database, an
unlisted tool and an invalid limit, and exercise the SDK stdio lifecycle. Two further
properties are worth checking by hand in your environment: over a raw stdio session every
stdout line should parse as JSON-RPC with diagnostics only on stderr, and a full
initialize → tools/list → tools/call sequence should succeed inside a network namespace with
no route out. The latter shows the server needs no external service; it says nothing about a
cloud model, which does need one.

If startup fails, check fixed absolute paths, `km validate`, rebuild the index, and installed
SDK version. Test in a terminal with the SDK client, not by mixing prints into stdout.

## 応答上限を使うクライアント（bounded mode）

既存の`km get`と既定のMCP 3ツールの返却型は変えない。コンテキストが小さい
クライアントでは明示的に次のモードを選び、クライアントのツール一覧を再取得する。

```bash
km-mcp --vault /outside/personal/vault --db /outside/personal/index.sqlite3 \
  --scope personal --include-candidates --bounded \
  --response-max-chars 12000 --response-max-bytes 24000
```

このプロセスが広告するのは`search_memory_bounded`、`get_memory_chunk`、
`build_context_bounded`の3つだけ。旧名へ全文取得をフォールバックしない。
旧名を直接呼ぶとサーバーが`unknown tool or invalid arguments`で拒否する。

MCPをHTTPへ変換するブリッジを挟む場合、ブリッジがinputSchemaから作る要求モデルで
未知の引数（`scope`や`db`等）を除去し、サーバーへ届く前に消えることがある。
その場合の正常応答は**サーバーによる拒否の証拠ではない**。SDK stdioでの直接呼び出しでは
サーバー自身が拒否する。この2層を分けて確認し、ブリッジ・版・設定ごとに記録する。
権限が起動時のscope/保存先に固定されていることと、追加引数へのHTTP応答を別々に確認する。
ブリッジがMCPエンベロープを外したHTTP本文のサイズも、サーバー側の上限対象とは区別する。
既定モードに上限オプションだけを指定しても有効にはならず、既定値以外なら拒否する。

上限は結果のJSON本文、metadata、それをtextとして包むMCPのcontent/isErrorエンベロープを
直列化したUnicode文字数とUTF-8 byte数の両方に適用する。JSONのエスケープも数える。
JSON-RPCのidや通信フレームは含まない。上限は起動時固定で、モデルから増やせない。
1応答の上限であり、会話全体の累積予算ではない。クライアントでは入力文・ツール定義・
過去の取得・生成枠・通信エンベロープの余裕を別に確保する。文字数をtoken数と呼ばない。

検索はID/hash、題名・状態・主張種別・出典・時点と該当断片を返す。
検索断片は正規化済みなので直接の引用には使わず、原文を部分取得する。
`omitted_results`は今回取得した上位件数内で応答上限により省いた数。全検索結果の件数ではない。
任意の大きなsource_metadata等はヘッダーへ複写しない。原文のfrontmatterから段階取得できる。
固定の出典情報さえ収まらない場合は明示的に拒否し、出典を黙って切らない。

`get_memory_chunk`はidentifierを指定して始め、next_cursorがある間は同じIDとそのcursorを渡す。
locatorは原文Markdown全体のUnicode codepointによる半開区間`[start,end)`で、先頭0・末尾total。
frontmatterも含む。連続するend/startを照合し、hashが同じ断片だけを連結すると原文と一致する。
next_cursorがnullになるまで末尾の表・用語集・出典も取得できる。引用にはID/hash/locatorを残す。
途中の原本変更・削除・利用保留・索引不整合では拒否される。変更後は再検索して先頭から取り直す。
検索のscope・候補設定は取得中も固定。cursorは権限やパスを指定するものではない。

contextは既存の省略表示を保ち、直列化結果が収まるまで本文予算を減らす。
最小のcontextヘッダーも収まらなければ拒否する。scope・保留・候補の制御は同じcoreを使う。

人工データと公式Python SDKのstdio試験で、実際のCallToolResult直列化を確認する。
特定のクライアント・ブリッジ・モデルでコンテキスト超過が改善するかは、このrepositoryでは検証しない。
導入時はクライアント各版、モデル、入力長・生成枠、上限、省略表示、末尾根拠への到達を
少数例で別々に記録する。
