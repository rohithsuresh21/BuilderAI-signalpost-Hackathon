import gzip, shutil
with gzip.open('brreg-enheter-new.csv.gz','rb') as fin, open('brreg-enheter.csv','wb') as fout:
    shutil.copyfileobj(fin,fout)
print('ok')
