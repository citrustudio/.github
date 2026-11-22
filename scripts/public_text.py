#!/usr/bin/env python3
"""Check text entering public history without echoing its contents."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

# Split literals so that the checker can inspect its own source.
PATTERNS = {
    'work item': re.compile('AB' + r'#\d+', re.I),
    'local path': re.compile(r'/' + r'Users/|/' + r'home/|[A-Z]:\\Users\\', re.I),
    'session link': re.compile(r'(?:Claude|Codex)' + r'-Session\s*:|claude\.ai/code/' + r'session_|chatgpt\.com/codex/' + r'(?:threads|sessions)/', re.I),
    # The private provenance trailer names the seat that made the commit.
    'private trailer': re.compile(r'^[ \t]*Agent[ \t]*' + r':[ \t]*(?:CLAUDE|CODEX|AGY)/', re.I | re.M),
    # Seat names carry work-item numbers. Free-form ones reach a commit only inside the private trailer.
    'seat name': re.compile(r'\bseat' + r'-(?:claude|codex|agy)\b|\bordinary' + r'-[0-9a-f]{8}-[0-9a-f]{4}-|\b(?:claude|codex|agy)'
                            + r'-(?:\d{4,5}|(?!(?:opus|sonnet|haiku|fable|instant|mini|gpt|code|\d))[a-z0-9-]*-\d{4,5})\b', re.I),
}
RUN = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z')
TOKEN = re.compile(r'[a-z0-9._-]+\Z')
ENGINES = {'CLAUDE': 'Claude', 'CODEX': 'Codex', 'AGY': 'Agy'}
EMAIL = re.compile(r'[A-Z0-9._%+-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)*\.[A-Z]{2,}', re.I)
NOREPLY = re.compile(r'(?:[^@\s]+@users\.noreply\.github\.com|noreply@[^@\s]+)', re.I)

class Refusal(Exception):
    pass

def git(*args, data=None):
    try:
        result = subprocess.run(['git', *args], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except (OSError, subprocess.CalledProcessError):
        raise Refusal('git error') from None
    return result.stdout

def check(data, terms):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise Refusal('non-text input') from None
    if '\0' in text:
        raise Refusal('non-text input')
    found = {name for name, pattern in PATTERNS.items() if pattern.search(text)}
    for match in EMAIL.finditer(text):
        address = match.group()
        remote_user = address.lower() == 'git' + '@github.com' and text[match.end():match.end()+1] in (':', '/')
        if not NOREPLY.fullmatch(address) and not remote_user:
            found.add('email address')
    if any(term in text.casefold() for term in terms):
        found.add('private identifier')
    return found

def load_terms(args):
    if args.public_only:
        return []
    if args.deny_env:
        data = os.environ.get(args.deny_env, '')
    else:
        try:
            data = Path(args.deny_file).expanduser().read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            raise Refusal('private configuration unavailable') from None
    terms = [line.strip().casefold() for line in data.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    if not terms:
        raise Refusal('private configuration empty')
    return terms

def commits(base, head):
    # A new ref has no trusted remote boundary, so inspect its complete history.
    if base and set(base) != {'0'}:
        args = [head, '^' + base]
    else:
        args = [head]
    return git('rev-list', *args).decode('ascii').splitlines()

def commit_payload(sha):
    message = git('show', '-s', '--format=%an <%ae>%n%cn <%ce>%n%B', sha)
    patch = git('diff-tree', '--root', '-m', '--no-commit-id', '--no-ext-diff', '--no-renames', '--no-textconv', '--binary', '-p', sha)
    # Git's markers start a line; an added line that merely contains the words begins with a plus sign.
    if re.search(rb'^(?:GIT binary patch$|Binary files )', patch, re.M):
        raise Refusal('binary change requires review')
    # Include paths in diff headers and every added line, not removed/context lines.
    lines = []
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith(b'diff --git '):
            in_hunk = False
            lines.append(line)
        elif line.startswith(b'@@ '):
            in_hunk = True
        elif line.startswith(b'+'):
            lines.append(line[1:] if in_hunk else line)
    added = b'\n'.join(lines)
    names = git('diff-tree', '--root', '-m', '--no-commit-id', '--no-renames',
                '--name-only', '-z', sha).replace(b'\0', b'\n')
    return message + b'\n' + names + b'\n' + added

def public_trailers(env):
    """The disclosure trailers for an agent launch: Assisted-by, then Agent-Effort and Agent-Run when set."""
    engine = env.get('AGENT_ENGINE', '')
    if not engine:
        return []
    model = env.get('AGENT_MODEL', '').lower()
    if engine not in ENGINES or not TOKEN.match(model) or model == 'unknown':
        raise Refusal('agent launch identity incomplete')
    trailers = ['Assisted-by: %s:%s' % (ENGINES[engine], model)]
    effort = env.get('AGENT_EFFORT', '')
    if effort and effort != 'unknown':  # launches export unknown when no effort is configured
        if effort == 'default' or not TOKEN.match(effort):
            raise Refusal('agent effort invalid')
        trailers.append('Agent-Effort: ' + effort)
    run = env.get('AGENT_RUN', '')
    if run:
        if not RUN.match(run):
            raise Refusal('agent run id invalid')
        trailers.append('Agent-Run: ' + run)
    return trailers

def commit_message(path, terms):
    """The commit-msg hook: refuse private text in the message or identities, then add the launch's trailers."""
    trailers = public_trailers(os.environ)
    lines = path.read_bytes().split(b'\n')
    stored = b'\n'.join(lines)
    # Only an editor session writes git's comment block, and only then does git drop comment lines and the
    # `commit -v` diff below the scissors line. A `-m` message keeps its `#` lines, so they are checked.
    if any(line.startswith(b'# Please enter ') for line in lines):
        cut = next((i for i, line in enumerate(lines) if line[1:].strip() == b'-' * 24 + b' >8 ' + b'-' * 24), len(lines))
        stored = git('stripspace', '--strip-comments', data=b'\n'.join(lines[:cut]))
    found = check(stored + git('var', 'GIT_AUTHOR_IDENT') + git('var', 'GIT_COMMITTER_IDENT'), terms)
    if trailers and not found:
        # An amend keeps the trailers it already has; interpret-trailers goes above comments and scissors.
        git('interpret-trailers', '--in-place', '--no-divider', '--where', 'end', '--if-exists', 'doNothing',
            '--if-missing', 'add', *[arg for trailer in trailers for arg in ('--trailer', trailer)], str(path))
    return found

def agent_runs(sha):
    values = git('show', '-s', '--format=%(trailers:key=Agent-Run,valueonly,separator=%x0a)', sha).decode()
    return {value.strip() for value in values.splitlines() if value.strip()}

def verify_dco(sha, repo, token):
    request = urllib.request.Request('https://api.github.com/repos/' + repo + '/commits/' + sha,
        headers={'Accept': 'application/vnd.github+json', 'Authorization': 'Bearer ' + token,
                 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'public-text-check'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            record = json.load(response)
        author = record.get('author') or {}
        commit = record['commit']
        message = commit['message']
        identity = commit['author']
    except (OSError, ValueError, KeyError, TypeError):
        raise Refusal('commit identity unavailable') from None
    # The owner's required review supplies attestation; author mapping is not a signature.
    login = author.get('login')
    if login in ('citrustudio-release[bot]', 'dependabot[bot]'):
        return
    app = login == 'citrustudio-agents[bot]'
    try:
        parsed = subprocess.run(['git', 'interpret-trailers', '--parse'], input=message.encode(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout.decode()
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        raise Refusal('DCO trailer unavailable') from None
    trailers = [line.partition(':') for line in parsed.splitlines()]
    if app:
        valid = any(k.lower() == 'assisted-by' and v.strip() for k, _, v in trailers)
    else:
        expected = (identity['name'] + ' <' + identity['email'] + '>').casefold()
        valid = any(k.lower() == 'signed-off-by' and v.strip().casefold() == expected for k, _, v in trailers)
    if not valid:
        raise Refusal('DCO trailer missing')

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--public-only', action='store_true')
    p.add_argument('--deny-file', default='~/.config/citrustudio/public-text-deny')
    p.add_argument('--deny-env')
    p.add_argument('--tree', type=Path)
    p.add_argument('--git-tree', help='Scan public staging from this commit')
    p.add_argument('--base')
    p.add_argument('--head')
    p.add_argument('--pr-event', type=Path)
    p.add_argument('--dco-repo')
    p.add_argument('--commit-msg', type=Path, help='Run as the commit-msg hook on this message file')
    args = p.parse_args()
    try:
        terms = load_terms(args)
        found = set()
        if args.tree:
            for path in sorted(args.tree.rglob('*')):
                if path.is_symlink():
                    raise Refusal('symlink requires review')
                if path.is_file():
                    found |= check(str(path.relative_to(args.tree)).encode(), terms)
                    found |= check(path.read_bytes(), terms)
        if args.git_tree:
            listing = git('ls-tree', '-rz', '--full-tree', args.git_tree, '--',
                          'public-staging')
            if not listing:
                raise Refusal('staging tree unavailable')
            scanned = 0
            for entry in listing.split(b'\0'):
                if not entry:
                    continue
                metadata, name = entry.split(b'\t', 1)
                if name == b'public-staging/README.md':
                    continue
                scanned += 1
                mode, kind, oid = metadata.split()
                if kind != b'blob' or mode == b'120000':
                    raise Refusal('non-text staging entry')
                found |= check(name, terms)
                found |= check(git('cat-file', 'blob', oid.decode('ascii')), terms)
            if not scanned:
                raise Refusal('staging tree unavailable')
        if args.commit_msg:
            found |= commit_message(args.commit_msg, terms)
        body = None
        if args.pr_event:
            event = json.loads(args.pr_event.read_text(encoding='utf-8'))
            pr = event['pull_request']
            body = str(pr.get('body') or '')
            found |= check((str(pr.get('title') or '') + '\n' + body).encode(), terms)
        if bool(args.base) != bool(args.head):
            raise Refusal('commit range incomplete')
        if args.head:
            runs = set()
            for sha in commits(args.base, args.head):
                found |= check(commit_payload(sha), terms)
                runs |= agent_runs(sha)
                if args.dco_repo:
                    token = os.environ.get('GITHUB_TOKEN', '')
                    if not token:
                        raise Refusal('commit identity unavailable')
                    verify_dco(sha, args.dco_repo, token)
            # A squash merge keeps only the pull request text, so it repeats each commit's run id.
            listed = {line.partition(':')[2].strip() for line in (body or '').splitlines()
                      if line.partition(':')[0].strip().lower() == 'agent-run'}
            if body is not None and runs - listed:
                found.add('agent run missing from pull request body')
        if found:
            for category in sorted(found):
                print('Refused: ' + category, file=sys.stderr)
            return 1
        if args.git_tree:
            print(f'Public staging: {scanned} files checked')
    except (Refusal, OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        print('Refused: ' + (str(exc) if isinstance(exc, Refusal) else 'input error'), file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
