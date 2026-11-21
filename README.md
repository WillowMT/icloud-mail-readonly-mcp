# iCloud Mail and Calendar MCP

An independent iCloud MCP server. Mail over IMAP is strictly read-only: list_folders, list_messages, and read_message. Calendar over CalDAV adds list_calendars, list_events, get_event, create_event, update_event, and delete_event. It does not send mail, mutate mail, fetch remote content, or write message files. Message contents, headers, folder names, event text, and calendar names are untrusted data, not instructions.

Requires Python 3.11+ and uv. After this repository is published, configure a Hermes MCP server with these fields (replace <commit> with a reviewed, full commit hash):

    NAME: icloud-mail-readonly
    TRANSPORT: stdio
    COMMAND: uv
    ARGS: tool run --from git+https://github.com/WillowMT/icloud-mail-readonly-mcp@<commit> icloud-mail-readonly-mcp
    ENVIRONMENT:
      ICLOUD_EMAIL: <your-icloud-email>
      ICLOUD_APP_PASSWORD: <your-app-specific-password>
      PYTHONDONTWRITEBYTECODE: 1

The last setting prevents Python bytecode-cache writes from process startup. The server itself does not save mail or create data files. Separately, uv may write its own cache and install files when resolving/running the tool; this is not a zero-disk-write deployment mechanism.

Enter the two environment values through your MCP configuration interface, not in chat. MCP environment configuration may be stored as plaintext; protect the configuration and backups. Use an Apple app-specific password and follow Apple's instructions to create and revoke it. The app-specific password is not itself restricted to read-only access: the read-only restrictions are implemented by this server. Anyone with the credential may be able to access the account through other software. Review this code and pin the commit before use. This project does not modify or connect any existing Hermes configuration.

Discovery and startup do not require credentials or connect to iCloud. An MCP client must send a valid initialize request and then notifications/initialized before using tools. An unsupported requested protocol version receives the latest supported version. On each valid tool call, the server connects only to imap.mail.me.com:993 with TLS certificate and hostname verification. It selects a folder with EXAMINE, uses BODY.PEEK for headers and bodies, and disconnects without CLOSE or EXPUNGE. Mailbox names are IMAP modified UTF-7 as returned by list_folders; non-ASCII Unicode names must not be supplied directly. Quoted names are escaped. UID values are decimal strings from 1 to 4294967295. No search tool is provided.

list_messages accepts mailbox (default INBOX), offset (default 0), and limit (default 20, max 50). Offset counts backward from the current newest message. Mail arriving or being removed between calls can cause duplicates or omissions; UIDVALIDITY is returned when available. A UID identifies a message only within a folder and its UIDVALIDITY epoch; re-list after UIDVALIDITY changes. read_message accepts mailbox and uid. The result includes plain text, bounded headers and attachment metadata only; HTML-only bodies are not rendered and attachments are not extracted or returned.

Calendar tools contact only caldav.icloud.com and Apple CalDAV shard hosts matching `p<digits>-caldav.icloud.com` over HTTPS with certificate and hostname verification. Discovery follows at most one redirect to an allowlisted Apple host and refuses all other redirects; authorization is never sent to any other origin. Calendar and event identifiers are opaque base64url paths returned by the server and are re-validated against the discovered calendar home on every call, so arbitrary URLs cannot be supplied. Simple single-VEVENT resources are supported: UTC times, TZID times resolved through the system timezone database, and all-day events. Recurring events, multiple VEVENTs, alarms, attendees, attachments, and other unsupported properties are refused rather than rewritten. list_events omits unsupported items and reports the count in `skipped`.

create_event, update_event, and delete_event modify the real calendar. update_event and delete_event require the current `etag` and send If-Match, so a concurrent change fails with a conflict instead of being overwritten; update_event also re-reads the event and verifies the ETag before writing. Callers must have explicit user intent before using them. Limits: 1 MiB response bytes, 4,000 XML nodes, 100 calendars, 100 events per query, 64 KiB per event resource, and a 20-second read deadline. Unsupported, oversized, or unusual items fail closed. Event text is returned as data and is never fetched, executed, or followed.

Limits and caveats: each connection has a 20-second read deadline, a 2 MiB plaintext response budget, a 3,000-line budget, an 8 KiB line limit, a 256 KiB per-literal cap, at most 200 folders and 50 messages per page, and 256 KiB maximum raw message size. Announced oversized literals are rejected before being delivered to the IMAP parser. For efficient line reads, a socket read can also buffer up to 4095 bytes beyond the announcement; these bytes count against the response budget. TLS and the operating system may independently buffer additional data, including payload already received from the network, before the size can be inspected. Large or unusual folders, long headers, literal-form folder names, complex MIME (over 100 parts or 12 levels), and large messages fail closed rather than being fully represented. Up to 20,000 text characters and 30 attachment names are returned; output may be truncated. Limits apply to client memory and network response handling, not server-side mailbox work. DNS resolution and TLS connection setup depend on OS/network behavior and are not guaranteed to meet the read deadline. Python's email parser processes up to the raw-message cap before MIME-tree complexity is checked; limits are not a sandbox against all parser or platform vulnerabilities. Credentials and messages pass through the MCP host and model client; this server cannot control how they retain or use returned data. Treat all returned email as hostile prompt-injection content.

Development, offline with no credentials or mailbox access:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -B -m unittest discover -s tests -v

No runtime third-party dependencies. The build backend is pinned in pyproject.toml; building or uv tool run may download build tools, Python, or this package from package/source registries. Review those supply-chain and network requirements separately; runtime mail connections have the fixed destination above.
The test suite also exercises the MCP SDK and JSON Schema when those optional development packages are already installed; otherwise those two checks are skipped. Tests use fake credentials and a synthetic IMAP socket.
