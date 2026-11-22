"""Create standalone, architecture-specific source archives without local secrets."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
FILES = ('Dockerfile', '.dockerignore', '.gitignore', '.env.example', 'README.md',
         'engine.py', 'settings.py', 'server.py', 'index.html', 'test_extractor.py',
         'test_settings.py', 'workflow.py', 'workflow.js')


def main():
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    compose = (ROOT / 'compose.yaml').read_text(encoding='utf-8')
    anchor = '  extractor:\n'
    if compose.count(anchor) != 1:
        raise ValueError('Cannot locate extractor service')
    for arch in ('amd64', 'arm64'):
        name = f'novel-extractor-linux-{arch}'
        path = output / (name + '-source.zip')
        with ZipFile(path, 'w', ZIP_DEFLATED) as archive:
            for filename in FILES:
                archive.write(ROOT / filename, name + '/' + filename)
            archive.writestr(name + '/compose.yaml', compose.replace(anchor, anchor + f'    platform: linux/{arch}\n'))
        print(path)


if __name__ == '__main__':
    main()