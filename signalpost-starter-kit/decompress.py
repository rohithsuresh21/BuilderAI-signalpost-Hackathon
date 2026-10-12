import gzip, shutil, sys
src = sys.argv[1]
dst = sys.argv[2]
with gzip.open(src, 'rb') as fin, open(dst, 'wb') as fout:
    shutil.copyfileobj(fin, fout)
print('ok')
