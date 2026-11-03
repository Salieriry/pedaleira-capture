# Pedaleira Capture

Gravador do ADC cru da Placa B, verificador do formato AUD1, conversor CSV para
WAV e gravador UAC da Placa A. Capturas e arquivos derivados ficam fora do Git.

## Instalação

```bash
git clone https://github.com/Salieriry/pedaleira-capture.git
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Para consultar profiling durante uma gravação, clone também
`pedaleira-tools` como pasta irmã. Se os repositórios estiverem em outro local,
configure `PEDALEIRA_TOOLS_DIR`.

No Linux, a interface gráfica precisa de Tk/Tkinter instalado pelo sistema.
O gravador USB de áudio requer as dependências opcionais de
`requirements-uac.txt`.

## Uso

Passe um caminho de saída explícito no modo automatizado:

```bash
python recorder_demo.py /caminho/take.csv --port /dev/ttyACM0 \
  --seconds 60 --session sessao01 --label 1 --conditions "guitarra passiva"
python verify_capture.py /caminho/take.csv
python dataset_wav.py /caminho/take.csv
```

Sem argumentos, `recorder_demo.py` abre a interface. Ela grava em
`~/pedaleira-datasets`; `PEDALEIRA_DATASET_DIR` permite escolher outra pasta.
`recorder_usb.py` captura o áudio processado pela Placa A, não o ADC cru usado
para treinamento.

Execute os testes locais com `python -m unittest test_capture_verify.py`.
