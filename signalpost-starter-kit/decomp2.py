import gzip, shutil
src = 'E:/projects/Builderr.ai hackathon/signalpost-starter-kit/brreg-enheter2.csv'
dst = 'E:/projects/Builderr.ai hackathon/signalpost-starter-kit/brreg-enheter.csv'
with gzip.open(src, 'rb') as fin, open(dst, 'wb') as fout:
    shutil.copyfileobj(fin, fout)
print('ok')
