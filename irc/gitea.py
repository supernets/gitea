#!/usr/bin/env python3
# gitea.py — relays public git.supernets.org webhook events to #dev on irc.supernets.org.

import asyncio
import hashlib
import hmac
import json
import pathlib
import ssl
import time


ENV = dict(line.split('=', 1) for line in pathlib.Path(__file__).with_name('.env').read_text().splitlines() if '=' in line and not line.startswith('#'))

IRC_HOST   = 'irc.supernets.org'
IRC_PORT   = 6697
IRC_NICK   = 'GITEA'
IRC_USER   = 'gitea'
IRC_REAL   = 'https://git.supernets.org'
IRC_CHAN   = '#dev'
NICKSERV   = ENV['NICKSERV']
JOIN_DELAY = 6                        # supernets refuses JOIN for the first 6 seconds
REGISTER   = 2 * 60 * 60              # seconds connected before registering an unregistered nick
SEND_DELAY = 0.6                      # seconds between lines, avoids flood kick
RECONNECT  = 15                       # seconds between reconnect attempts

HTTP_HOST = '0.0.0.0'                 # container only has incusbr0, reachable from the host + gitea docker
HTTP_PORT = 9800
SECRET    = ENV['SECRET']             # must match the gitea webhook secret

MAX_COMMITS = 3                       # commits listed per push before summarizing
MAX_LEN     = 400                     # truncate lines to stay under the 512b IRC limit

ACTIONS = {                           # event -> actions worth announcing, None means all
	'create'        : None,
	'issue_comment' : ('created',),
	'issues'        : ('opened', 'closed', 'reopened'),
	'pull_request'  : ('opened', 'closed', 'reopened'),
	'push'          : None,
	'release'       : ('published',),
	'repository'    : ('created',),
}

BOLD  = '\x02'
RESET = '\x0f'
WHITE, BLACK, BLUE, GREEN, RED, BROWN   = '00', '01', '02', '03', '04', '05'
PURPLE, ORANGE, YELLOW, LGREEN, CYAN    = '06', '07', '08', '09', '10'
LCYAN, LBLUE, PINK, GREY, LGREY         = '11', '12', '13', '14', '15'

QUEUE: asyncio.Queue = asyncio.Queue()


def color(text: str, code: str) -> str:
	'''
	Wrap text in an mIRC color code.

	:param text: the text to colorize
	:param code: two-digit mIRC color number
	'''

	return f'\x03{code}{text}{RESET}'


def format_event(event: str, body: dict) -> list[str]:
	'''
	Render a Gitea webhook payload as colored IRC lines.

	:param event: value of the X-Gitea-Event header
	:param body: decoded JSON webhook payload
	'''

	repo   = body.get('repository', {}).get('full_name', '?')
	sender = body.get('sender', {}).get('username') or body.get('sender', {}).get('login', '?')
	tag    = f'{color("[", GREY)}{color(repo, BLUE)}{color("]", GREY)}'
	lines  = []

	if event == 'push':
		commits = body.get('commits', [])
		if not commits:
			return []
		branch  = body.get('ref', '').rsplit('/', 1)[-1]
		total   = body.get('total_commits') or len(commits)
		plural  = 'commit' if total == 1 else 'commits'
		lines.append(f'{tag} {color(sender, ORANGE)} pushed {BOLD}{total}{RESET} {plural} to {color(branch, GREEN)}')
		for commit in commits[:MAX_COMMITS]:
			short = commit.get('id', '')[:7]
			msg   = commit.get('message', '').splitlines()[0] if commit.get('message') else ''
			who   = commit.get('author', {}).get('name', '?')
			lines.append(f'{tag} {color(short, YELLOW)} {color(who, GREY)}: {msg}')
		if len(commits) > MAX_COMMITS:
			lines.append(f'{tag} {color(f"... and {len(commits) - MAX_COMMITS} more", GREY)}')

	elif event == 'repository':
		lines.append(f'{tag} {color(sender, ORANGE)} {color("created", GREEN)} repository {body.get("repository", {}).get("html_url", "")}')

	elif event == 'pull_request':
		pr     = body.get('pull_request', {})
		action = body.get('action', '?')
		if action == 'closed':
			action = 'merged' if pr.get('merged') else 'closed'
		shade = {'opened': GREEN, 'merged': PURPLE, 'closed': RED, 'reopened': YELLOW}.get(action, CYAN)
		lines.append(f'{tag} {color(sender, ORANGE)} {color(action, shade)} PR {color("#" + str(pr.get("number", "?")), YELLOW)}: {pr.get("title", "")} {pr.get("html_url", "")}')

	elif event == 'issues':
		issue  = body.get('issue', {})
		action = body.get('action', '?')
		shade  = {'opened': GREEN, 'closed': RED, 'reopened': YELLOW}.get(action, CYAN)
		lines.append(f'{tag} {color(sender, ORANGE)} {color(action, shade)} issue {color("#" + str(issue.get("number", "?")), YELLOW)}: {issue.get("title", "")} {issue.get("html_url", "")}')

	elif event == 'issue_comment':
		issue = body.get('issue', {})
		note  = body.get('comment', {}).get('body', '').splitlines()
		lines.append(f'{tag} {color(sender, ORANGE)} commented on {color("#" + str(issue.get("number", "?")), YELLOW)}: {note[0] if note else ""}')

	elif event == 'release':
		release = body.get('release', {})
		lines.append(f'{tag} {color(sender, ORANGE)} {color("published", PURPLE)} release {color(release.get("tag_name", "?"), GREEN)} {release.get("html_url", "")}')

	elif event == 'create':
		lines.append(f'{tag} {color(sender, ORANGE)} {color("created", GREEN)} {body.get("ref_type", "ref")} {color(body.get("ref", "?"), YELLOW)}')

	return [line[:MAX_LEN] for line in lines]


async def handle_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
	'''
	Accept one Gitea webhook delivery and queue its IRC lines.

	:param reader: incoming stream
	:param writer: outgoing stream
	'''

	try:
		head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout=10)
		headers = {}
		for raw in head.decode('latin-1').split('\r\n')[1:]:
			if ':' in raw:
				key, _, val = raw.partition(':')
				headers[key.strip().lower()] = val.strip()
		body = await asyncio.wait_for(reader.readexactly(int(headers.get('content-length', 0))), timeout=10)
		digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
		if not hmac.compare_digest(digest, headers.get('x-gitea-signature', '')):
			writer.write(b'HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
			await writer.drain()
			return
		event   = headers.get('x-gitea-event', '?')
		payload = json.loads(body)
		allowed = ACTIONS.get(event, ())
		if not is_public(payload):
			print(f'[http] skipped {event} (not public)', flush=True)
		elif allowed is not None and payload.get('action') not in allowed:
			print(f'[http] skipped {event} {payload.get("action")}', flush=True)
		else:
			for line in format_event(event, payload):
				await QUEUE.put(line)
			print(f'[http] queued {event}', flush=True)
		writer.write(b'HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
		await writer.drain()
	except Exception as err:
		print(f'[http] {err}', flush=True)
	finally:
		writer.close()


def is_public(body: dict) -> bool:
	'''
	True only for a public repo whose owner (user or org) is also public.

	:param body: decoded JSON webhook payload
	'''

	repo = body.get('repository')
	if not repo or repo.get('private', True) or repo.get('internal'):
		return False
	return repo.get('owner', {}).get('visibility', 'private') == 'public'


async def main():
	'''Start the webhook listener and the IRC client.'''

	server = await asyncio.start_server(handle_http, HTTP_HOST, HTTP_PORT)
	print(f'[http] listening on {HTTP_HOST}:{HTTP_PORT}', flush=True)
	async with server:
		await run_irc()


async def register_later(writer: asyncio.StreamWriter, connected: float):
	'''
	Register the nick with NickServ once connected for REGISTER seconds, then identify.

	:param writer: connected IRC stream
	:param connected: monotonic time the connection was made
	'''

	wait = max(0, connected + REGISTER - time.monotonic())
	print(f'[irc] nick not registered, registering in {wait:.0f}s', flush=True)
	await asyncio.sleep(wait)
	writer.write(f'PRIVMSG NickServ :REGISTER {NICKSERV}\r\n'.encode())
	await writer.drain()
	await asyncio.sleep(3)
	writer.write(f'PRIVMSG NickServ :IDENTIFY {NICKSERV}\r\n'.encode())
	await writer.drain()
	print('[irc] sent NickServ REGISTER + IDENTIFY', flush=True)


async def run_irc():
	'''Maintain the IRC connection, identifying, joining the channel and draining the queue.'''

	ctx = ssl.create_default_context()

	while True:
		tasks = []
		try:
			reader, writer = await asyncio.open_connection(IRC_HOST, IRC_PORT, ssl=ctx)
			connected = time.monotonic()
			print(f'[irc] connected to {IRC_HOST}:{IRC_PORT}', flush=True)
			writer.write(f'NICK {IRC_NICK}\r\nUSER {IRC_USER} 0 * :{IRC_REAL}\r\n'.encode())
			await writer.drain()
			while True:
				raw = await reader.readline()
				if not raw:
					break
				line = raw.decode('utf-8', 'replace').strip()
				if line.startswith('PING'):
					writer.write(f'PONG {line.split(maxsplit=1)[1]}\r\n'.encode())
					await writer.drain()
				elif line.startswith(':NickServ!') and ' NOTICE ' in line:
					text = line.split(' :', 1)[-1]
					print(f'[irc] NickServ: {text}', flush=True)
					if ('not registered' in text.lower() or 'not a registered' in text.lower()) and not any(t.get_name() == 'register' for t in tasks if not t.done()):
						tasks.append(asyncio.create_task(register_later(writer, connected), name='register'))
				elif line.split(' ')[1:2] == ['001']:
					tasks.append(asyncio.create_task(run_sender(writer)))
		except Exception as err:
			print(f'[irc] {err}', flush=True)
		finally:
			for task in tasks:
				task.cancel()
		print(f'[irc] reconnecting in {RECONNECT}s', flush=True)
		await asyncio.sleep(RECONNECT)


async def run_sender(writer: asyncio.StreamWriter):
	'''
	Identify, join the channel after the join delay, then relay queued lines to it.

	:param writer: connected IRC stream
	'''

	writer.write(f'PRIVMSG NickServ :IDENTIFY {NICKSERV}\r\n'.encode())
	await writer.drain()
	await asyncio.sleep(JOIN_DELAY)
	writer.write(f'JOIN {IRC_CHAN}\r\n'.encode())
	await writer.drain()
	print(f'[irc] joined {IRC_CHAN}', flush=True)
	while True:
		line = await QUEUE.get()
		writer.write(f'PRIVMSG {IRC_CHAN} :{line}\r\n'.encode('utf-8', 'replace'))
		await writer.drain()
		print(f'[irc] > {line}', flush=True)
		await asyncio.sleep(SEND_DELAY)



if __name__ == '__main__':
	asyncio.run(main())
