# Session directory and connection requests (0.9.4)

**Approved by Narek, 2026-11-16.** Build after 0.9.3 (admin session message opens a reply door), on top of it.

**Goal:** every member (and their agent) can see a list of the team's active sessions, without any content, and ask to be connected to one. The request goes to the manager and to the admin; only the admin approves. Approval opens a session-to-session door, so the two sessions can talk directly until it is closed.

## What a member sees
- Their own sessions and the manager, as today (reachable).
- Below them, **Others**: every other online session. Each shows its label/name, whose it is, the machine and online state. The project shows only under the existing rule (the `_shown` / `visible` rule; the masked "(private)" project otherwise). No messages, no activity text, no keys.
- The rows are visibly locked (a lock icon, muted). Clicking one opens a small panel:
  - **Request connection**;
  - which of my sessions to connect (preselected when the member has one);
  - an optional reason;
  - **Send**.
  - A pending request shows as "Requested" on the row; a denied one shows the reason.
- The agent side gets the same through a tool. Either `who` lists the others as not reachable, or a new tool does; asking goes through `request_access` with a session target, or a new `request_connect(session, reason)`. Pick the smaller design and say which.

## What happens on a request
- It is stored as a request, the same family as the project and machine requests. The admin sees it under Admin > Access > **Requests** with Approve / Deny (optional reason), and a badge on the Admin tab.
- The manager gets a notice, so it can tell the admin and answer the member. The manager cannot approve.
- **Approve** opens a `scope='session'` door between the requesting session and the target session (never a group door), and notifies both sessions and the requester's page.
- **Deny** sends the reason to the requester.
- Both are logged in plain words. An approved door shows in Admin > Access > Doors and closes the usual ways (door_close, the admin, person removal).
- Nothing becomes reachable before approval: `_listed` and `_may_message` stay the gate. The directory is a separate, content-free listing.

## Refuse
- Asking for your own session, or for the manager (always reachable).
- Asking for a session already behind an open door with yours.
- A duplicate pending request.
- A session that went offline while the request was pending: approval says so and opens no door.

## Tests
- Privacy: the directory gives no message, activity or key; the project is masked under the existing rule; a member still cannot message or `who` an "Others" session before approval.
- The request → approve → door → both can message flow, from the page and from the agent tool.
- The deny flow; the duplicate and own-session refusals; the manager notified but unable to approve.
- One slow page test: a member sees Others locked, requests, the admin approves, the row becomes a normal chat.

Version 0.9.4: README (the "Groups and doors" and "People and access" sections, Home paragraph) and CHANGELOG.
