p = r'venv\Lib\site-packages\speechbrain\utils\fetching.py'
c = open(p).read()

old = '    except HTTPError as e:\n        if "404 Client Error" in str(e):\n            raise ValueError("File not found on HF hub") from e\n        raise'
new = '    except HTTPError as e:\n        if "404 Client Error" in str(e):\n            raise ValueError("File not found on HF hub") from e\n        raise\n    except Exception as e:\n        if "404" in str(e) or "Entry Not Found" in str(e) or "RemoteEntryNotFoundError" in str(e):\n            return None\n        raise'

if old in c:
    c = c.replace(old, new)
    open(p, 'w').write(c)
    print('Done - patched successfully')
else:
    print('ERROR: pattern not found - printing except block context:')
    for i, line in enumerate(c.splitlines()):
        if 'except' in line and 'HTTPError' in line:
            lines = c.splitlines()
            for j in range(max(0, i-2), min(len(lines), i+8)):
                print(f'  {j}: {lines[j]}')
