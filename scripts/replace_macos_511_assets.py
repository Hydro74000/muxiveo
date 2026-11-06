"""One-off replacement of macOS assets on the existing stable 5.1.1 release."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

REPO = 'Hydro74000/muxiveo'
TAG = 'v5.1.1'
RELEASE_ID = 430675119
MAC_NAMES = ['Muxiveo-5.1.1.dmg', 'Muxiveo-5.1.1-macos.zip', 'Muxiveo-5.1.1-homebrew-macos.tar.gz']
NEW = Path('release-assets')
BACKUP = Path('publication-backup')
BACKUP.mkdir(exist_ok=True)


def gh(*args, payload=None, token=None):
    env = dict(os.environ)
    if token:
        env['GH_TOKEN'] = token
    command = ['gh', *args]
    if payload is not None:
        command += ['--input', '-']
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True, env=env, check=True)
    return result.stdout


def api(path, method='GET', payload=None, token=None):
    return json.loads(gh('api', '--method', method, path, payload=payload, token=token))


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


assert os.environ['GITHUB_REPOSITORY'].lower() == REPO.lower()
report = json.loads((NEW / 'macos-validation.json').read_text())
assert report['source_sha'] == os.environ['GITHUB_SHA'] and report['version'] == '5.1.1'
assert report['architecture'] == 'arm64' and report['codesign_deep_strict']
assert report['verified'] == ['app', 'dmg', 'zip', 'homebrew']
original = api(f'repos/{REPO}/releases/tags/{TAG}')
assert original['id'] == RELEASE_ID and not original['draft'] and not original['prerelease']
assert api(f'repos/{REPO}/releases/latest')['id'] == RELEASE_ID
original_tag = api(f'repos/{REPO}/git/ref/tags/{TAG}')
original_assets = {a['name']: a for a in original['assets']}
assert all(name in original_assets for name in MAC_NAMES + ['SHA256SUMS', 'Muxiveo.rb'])
(BACKUP / 'release.json').write_text(json.dumps(original, indent=2) + '\n')
(BACKUP / 'tag.json').write_text(json.dumps(original_tag, indent=2) + '\n')
gh('release', 'download', TAG, '--repo', REPO, '--pattern', 'SHA256SUMS', '--pattern', 'Muxiveo.rb', '--dir', str(BACKUP))
gh('release', 'download', 'update-feed', '--repo', REPO, '--pattern', 'stable.json', '--dir', str(BACKUP))
checksums = {}
for line in (BACKUP / 'SHA256SUMS').read_text().splitlines():
    digest, name = line.split(maxsplit=1)
    assert re.fullmatch('[0-9a-f]{64}', digest)
    checksums[name.removeprefix('*')] = digest
assert all(name in checksums for name in MAC_NAMES + ['Muxiveo.rb'])
for name in MAC_NAMES:
    assert (NEW / name).is_file() and (NEW / name).stat().st_size > 1000000
    checksums[name] = sha(NEW / name)

old_formula = (BACKUP / 'Muxiveo.rb').read_text()
pattern = r'(?m)(^  on_macos do\n    url "https://github.com/Hydro74000/muxiveo/releases/download/v5\.1\.1/Muxiveo-5\.1\.1-homebrew-macos\.tar\.gz"\n    sha256 ")[0-9a-f]{64}(?=")'
new_digest = checksums['Muxiveo-5.1.1-homebrew-macos.tar.gz']
new_formula, count = re.subn(pattern, lambda match: match[1] + new_digest, old_formula)
assert count == 1, 'Unexpected release Homebrew formula'
(NEW / 'Muxiveo.rb').write_text(new_formula)
checksums['Muxiveo.rb'] = sha(NEW / 'Muxiveo.rb')
(NEW / 'SHA256SUMS').write_text(''.join(f'{checksums[name]}  {name}\n' for name in sorted(checksums)))
feed = json.loads((BACKUP / 'stable.json').read_text())
assert feed['tag'] == TAG and feed['version'] == '5.1.1'

# Prepare the configured tap update before replacing any published file.
tap = os.environ.get('HOMEBREW_TAP_REPO') or 'Hydro74000/homebrew-mediarecode'
assert tap == 'Hydro74000/homebrew-mediarecode'
tap_token = os.environ['HOMEBREW_TAP_TOKEN']
assert tap_token, 'Missing token to synchronize the Homebrew checksum'
tap_path = f'repos/{tap}/contents/Formula/Muxiveo.rb'
tap_file = api(tap_path, token=tap_token)
tap_old = base64.b64decode(tap_file['content']).decode()
tap_new, count = re.subn(pattern, lambda match: match[1] + new_digest, tap_old)
assert count == 1, 'Configured tap no longer targets 5.1.1'
(BACKUP / 'tap-Muxiveo.rb').write_text(tap_old)
assert api(f'repos/{REPO}/releases/latest')['id'] == RELEASE_ID
assert {a['name']: a['id'] for a in api(f'repos/{REPO}/releases/{RELEASE_ID}')['assets']} == {name: a['id'] for name, a in original_assets.items()}

# --clobber deletes and replaces only these assets on the already existing release.
for name in MAC_NAMES + ['Muxiveo.rb', 'SHA256SUMS']:
    print(f'Replacing {name}', flush=True)
    gh('release', 'upload', TAG, str(NEW / name), '--repo', REPO, '--clobber')

updated = api(f'repos/{REPO}/releases/{RELEASE_ID}')
updated_assets = {a['name']: a for a in updated['assets']}
for name in MAC_NAMES + ['Muxiveo.rb', 'SHA256SUMS']:
    assert updated_assets[name]['digest'] == f'sha256:{sha(NEW / name)}', name
    assert updated_assets[name]['size'] == (NEW / name).stat().st_size
for entry in feed['assets']:
    entry['size'] = updated_assets[entry['name']]['size']
feed_path = NEW / 'stable.json'
feed_path.write_text(json.dumps(feed, indent=2, ensure_ascii=False) + '\n')
gh('release', 'upload', 'update-feed', str(feed_path), '--repo', REPO, '--clobber')
if tap_new != tap_old:
    api(tap_path, method='PUT', token=tap_token, payload={
        'message': 'fix(macos): refresh checksum for repaired Muxiveo 5.1.1 bundle',
        'sha': tap_file['sha'], 'content': base64.b64encode(tap_new.encode()).decode(),
    })

assert updated['id'] == original['id'] and updated['tag_name'] == original['tag_name']
assert updated['body'] == original['body'] and updated['published_at'] == original['published_at']
assert api(f'repos/{REPO}/releases/latest')['id'] == RELEASE_ID
assert api(f'repos/{REPO}/git/ref/tags/{TAG}')['object'] == original_tag['object']
replaced = set(MAC_NAMES + ['Muxiveo.rb', 'SHA256SUMS'])
assert {name: a['id'] for name, a in updated_assets.items() if name not in replaced} == {name: a['id'] for name, a in original_assets.items() if name not in replaced}
result = {'release_id': RELEASE_ID, 'tag': TAG, 'latest': True, 'tag_unchanged': True,
          'unchanged_other_assets': True, 'source_sha': report['source_sha'],
          'replaced': {name: {'id': updated_assets[name]['id'], 'sha256': sha(NEW / name)} for name in sorted(replaced)},
          'homebrew_tap': tap, 'stable_feed_updated': True}
Path('publication-result.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
    summary.write('Existing release v5.1.1 repaired; release ID, tag, latest status and other platform assets preserved.\n\n')
    summary.write('Native codesign verification passed for app, DMG, ZIP and Homebrew archive.\n')
