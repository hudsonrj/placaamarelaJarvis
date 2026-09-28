# MySoul na placa

A placa é a Cheap Yellow Display em `firmware/mysoul_cyd`. O cérebro é `gateway/server.py`: descobre a placa por UDP, fala com o Atomic Chat em `127.0.0.1:1337`, busca o projeto MySoul no banco local do Atomic Chat e toca o áudio nos dispositivos do Windows.

Não copiar os PDFs do MySoul para o repositório. A página de MCP é `gateway/page.html`, servida na porta 8791 e espelhada pelo IP da placa.

Drivers da tela e do toque estão em `firmware/libraries`.
