# Roadmap

Where BEA is, what is still open, where it is going, and what it will deliberately never
do. [Português (Brasil)](#português-brasil) at the end.

## Status: version 0.1.0

| Area | State |
| :-- | :-- |
| Input: an already-extracted folder (ExeFS and/or RomFS) | done |
| `inspect`, `analyze`, `build`, `profile validate / init`, `nso info / image / hunk`, `synth`, `doctor` | done |
| Formats: NSO0 (LZ4, hashes, build ID), IPS / IPS32, NPDM, NACP, DDS, image headers | done |
| Textures: loose PNG, TGA, DDS (mip dropping for any pixel format; resampling for the formats BEA can encode) | done |
| Configuration edits (INI, JSON, regular expressions) | done |
| Executable patches verified by hash windows, written as IPS | done |
| Video transcoding through a user-installed ffmpeg | done, less field-tested |
| Output: LayeredFS (`atmosphere` layout) and a flat mod folder | done |
| 3,900+ synthetic tests, strict typing, architecture guardrails | done |

## Pending work

Open items, in rough order of priority. Help with any of them is welcome.

- [ ] **Independent review of the format modules.** `nso`, `ips`, `dds`, `imageinfo`,
      `nacp`, `npdm` and `pfs0` were written from public documentation and are covered
      by thousands of synthetic tests, but those tests were written by their authors. An
      adversarial review by someone else (malformed inputs, boundary values, every claim
      of `docs/FORMATS.md`) has not happened yet.
- [ ] **First test on real hardware.** The folder layout and the IPS offsets follow
      public documentation and are verified byte for byte on synthetic data. No build has
      yet been tried by the project on a console or in a mod loader.
- [ ] **First community profile.** `profiles/` holds only the template. The first real
      profiles will show which engine features are missing.
- [ ] **First CI run on GitHub.** The workflow covers Linux and Windows with Python 3.12,
      3.13 and 3.14; so far the suite ran locally on Windows with Python 3.12 and 3.14.
- [ ] **Field testing of the media transformer** with more codecs and containers.
- [ ] **Agent manifests for other tools.** The Claude Code plugin manifest passed
      `claude plugin validate`; manifests for other coding agents are not written yet.
- [ ] **A first release**: tag `v0.1.0`, a GitHub release, then a package on PyPI.
- [ ] **A legal review by a qualified lawyer** (Brazil and the United States). The
      project's policy comes from engineering research into how comparable tools have
      been treated; it is not legal advice.

### Known limitations of 0.1

- Only **loose** RomFS files are processed. Textures inside engine containers (NX texture
  containers, SARC archives, Unity, Unreal Engine) are listed by `bea analyze` and left
  untouched.
- **No encoder** for BC4, BC6H, BC7 or ASTC: such textures are mip-dropped when they have
  a mip chain and reported as unsupported otherwise. BC1, BC3 and BC5 are re-encoded with
  a basic-quality built-in encoder.
- Cube maps, texture arrays and volume textures are mip-dropped but never resampled;
  16-bit PNGs are not resampled.
- Executable modules compressed with the newer zstd-based scheme ("ZBIC") are refused.
- LayeredFS cannot delete a file of the game (a limit of the folder layout, not of BEA).
- Some format facts could only be checked against documentation, not against real files:
  the module-name region of retail executables and the compressed title block of recent
  `control.nacp` files. `docs/FORMATS.md` and the parsers stay tolerant there.

## Next: asset formats

All of this works on plain files and needs no keys.

1. **NX texture containers (BNTX).** Read the container, report every texture, and drop
   top mip levels without decoding or re-tiling (the block-linear layout allows it).
2. **Block-linear tiling.** Untile / tile, so that textures can be resampled, not only
   mip-dropped.
3. **ASTC and better BCn encoding** through external encoders located at run time (never
   bundled), behind the same "unsupported unless available" rule as ffmpeg.
4. **Archives that only wrap files** (uncompressed and commonly compressed first-party
   style archives), so that the textures inside become reachable.
5. **Engine detection by file signatures** instead of file names.

## Later: engine containers

Unity and Unreal Engine containers, limited to what is stored in the clear. No
game-specific keys in profiles, no bundled proprietary decompressors.

## Ideas, not commitments

- A tool server (MCP) so that coding agents can drive BEA directly.
- Export of verified patches to the text patch format some mod loaders read.
- Verification windows with wildcards, to carry a patch across builds.
- A small community index of profiles, built from this repository only.

## Deliberately out of scope

These were part of the first plan for BEA. Research into how tools of this kind have been
treated changed the plan; the reasoning is kept here so the decision is not re-opened by
accident. None of this is legal advice.

| Not planned | Why |
| :-- | :-- |
| Reading packaged dumps (`.nsp`, `.xci`, `.nsz`, …) | It requires decrypting with console keys. That act, not the copying of any content, is what removal requests against comparable open-source projects have been based on, down to the level of a format library. Starting from an extracted folder keeps BEA entirely on the side of plain files. |
| Handling keys in any form | Same reason. BEA has no code path that reads a key file, and a test keeps it that way. |
| Rebuilding installable packages | The output would be a complete copy of a game in its distribution format, and running it requires defeating signature checks. LayeredFS already covers personal use. |
| Patches that defeat a protection | Signature, licence, ticket, entitlement, online, anti-tamper or integrity checks, region locks, and platform, hardware-model or firmware-version gates. BEA adapts how a game performs on hardware it already targets; it does not make software run where its publisher did not release it. |
| Patching system software | Profiles target the game's own modules, inside the user's own game folder. |
| Downloading anything (games, profiles, patches, tools) | BEA is offline. Community patch databases without a license are not vendored or fetched. |
| Shipping game-derived files | Outputs stay with the user; the repository ships recipes (profiles), never results. |

If your use case needs one of these, it needs a different tool.

---

## Português (Brasil)

*Tradução do texto acima. Em caso de divergência, vale a versão em inglês.*

### Situação: versão 0.1.0

| Área | Situação |
| :-- | :-- |
| Entrada: uma pasta já extraída (ExeFS e/ou RomFS) | pronto |
| `inspect`, `analyze`, `build`, `profile validate / init`, `nso info / image / hunk`, `synth`, `doctor` | pronto |
| Formatos: NSO0 (LZ4, hashes, build ID), IPS / IPS32, NPDM, NACP, DDS, cabeçalhos de imagem | pronto |
| Texturas: PNG, TGA e DDS soltos (remoção de níveis de mip para qualquer formato de pixel; reamostragem para os formatos que o BEA sabe codificar) | pronto |
| Edição de configurações (INI, JSON, expressões regulares) | pronto |
| Patches de executável verificados por janelas de hash, gravados como IPS | pronto |
| Transcodificação de vídeo com um ffmpeg instalado pelo usuário | pronto, menos testado em campo |
| Saída: LayeredFS (layout `atmosphere`) e pasta de mod plana | pronto |
| Mais de 3.900 testes sintéticos, tipagem estrita, travas de arquitetura | pronto |

### Pendências

Itens em aberto, mais ou menos em ordem de prioridade. Ajuda em qualquer um é bem-vinda.

- [ ] **Revisão independente dos módulos de formato.** `nso`, `ips`, `dds`, `imageinfo`,
      `nacp`, `npdm` e `pfs0` foram escritos a partir de documentação pública e têm
      milhares de testes sintéticos, mas esses testes foram escritos pelos próprios
      autores. Uma revisão adversarial feita por outra pessoa (entradas malformadas,
      valores-limite, cada afirmação do `docs/FORMATS.md`) ainda não aconteceu.
- [ ] **Primeiro teste em hardware real.** O layout de pastas e os offsets de IPS seguem a
      documentação pública e são verificados byte a byte com dados sintéticos. O projeto
      ainda não testou nenhum build num console ou num carregador de mods.
- [ ] **Primeiro perfil da comunidade.** `profiles/` só tem o template. Os primeiros perfis
      reais vão mostrar quais recursos faltam no motor.
- [ ] **Primeira execução da CI no GitHub.** O workflow cobre Linux e Windows com Python
      3.12, 3.13 e 3.14; até agora a suíte rodou localmente no Windows com 3.12 e 3.14.
- [ ] **Testes em campo do transformador de mídia** com mais codecs e contêineres.
- [ ] **Manifestos para outros agentes.** O manifesto de plugin do Claude Code passou no
      `claude plugin validate`; os de outros agentes de código ainda não existem.
- [ ] **Uma primeira release**: tag `v0.1.0`, uma release no GitHub e depois um pacote no PyPI.
- [ ] **Uma revisão jurídica por um advogado qualificado** (Brasil e Estados Unidos). A
      política do projeto vem de pesquisa de engenharia sobre como ferramentas comparáveis
      foram tratadas; não é aconselhamento jurídico.

#### Limitações conhecidas da 0.1

- Só arquivos **soltos** do RomFS são processados. Texturas dentro de contêineres de
  engine (contêineres de textura NX, arquivos SARC, Unity, Unreal Engine) são listadas
  pelo `bea analyze` e deixadas intactas.
- **Sem codificador** para BC4, BC6H, BC7 ou ASTC: essas texturas têm níveis de mip
  removidos quando possuem cadeia de mips, e são relatadas como não suportadas caso
  contrário. BC1, BC3 e BC5 são recodificados com um codificador embutido de qualidade
  básica.
- Cube maps, arrays de textura e texturas de volume têm mips removidos, mas nunca são
  reamostrados; PNGs de 16 bits não são reamostrados.
- Módulos executáveis comprimidos com o esquema mais novo baseado em zstd ("ZBIC") são
  recusados.
- O LayeredFS não consegue apagar um arquivo do jogo (limite do layout de pastas, não do BEA).
- Alguns fatos de formato só puderam ser conferidos na documentação, não em arquivos reais:
  a região de nome de módulo de executáveis de varejo e o bloco de títulos comprimido de
  arquivos `control.nacp` recentes. O `docs/FORMATS.md` e os parsers continuam tolerantes
  nesses pontos.

### Próximo: formatos de assets

Tudo isto funciona sobre arquivos comuns e não precisa de chaves.

1. **Contêineres de textura NX (BNTX).** Ler o contêiner, relatar cada textura e remover
   níveis de mip do topo sem decodificar nem refazer o tiling (o layout block-linear permite).
2. **Tiling block-linear.** Desfazer / refazer o tiling, para que texturas possam ser
   reamostradas, e não só ter mips removidos.
3. **ASTC e codificação BCn melhor** com codificadores externos localizados em tempo de
   execução (nunca embutidos), sob a mesma regra do ffmpeg: "não suportado se não estiver
   disponível".
4. **Arquivos que só empacotam arquivos** (sem compressão ou com compressões comuns), para
   que as texturas dentro deles fiquem acessíveis.
5. **Detecção de engine por assinaturas de arquivo** em vez de nomes.

### Depois: contêineres de engine

Contêineres de Unity e Unreal Engine, limitados ao que está guardado em claro. Sem chaves
específicas de jogo nos perfis, sem descompressores proprietários embutidos.

### Ideias, não compromissos

- Um servidor de ferramentas (MCP) para que agentes de código usem o BEA diretamente.
- Exportar patches verificados para o formato de patch em texto que alguns carregadores de mods leem.
- Janelas de verificação com curingas, para levar um patch de um build para outro.
- Um pequeno índice comunitário de perfis, construído só a partir deste repositório.

### Deliberadamente fora do escopo

Isto fazia parte do primeiro plano do BEA. A pesquisa sobre como ferramentas desse tipo
foram tratadas mudou o plano; o raciocínio fica registrado aqui para que a decisão não
seja reaberta por engano. Nada disto é aconselhamento jurídico.

| Não planejado | Por quê |
| :-- | :-- |
| Ler dumps empacotados (`.nsp`, `.xci`, `.nsz`, …) | Exige descriptografar com chaves do console. Foi esse ato, e não a cópia de conteúdo, a base dos pedidos de remoção contra projetos de código aberto comparáveis, chegando até uma biblioteca de formatos. Começar de uma pasta já extraída mantém o BEA inteiramente do lado dos arquivos comuns. |
| Manipular chaves de qualquer forma | Mesmo motivo. O BEA não tem nenhum caminho de código que leia um arquivo de chaves, e um teste garante isso. |
| Reconstruir pacotes instaláveis | A saída seria uma cópia completa de um jogo no formato de distribuição, e rodá-la exige burlar verificações de assinatura. O LayeredFS já cobre o uso pessoal. |
| Patches que burlam uma proteção | Verificações de assinatura, licença, ticket, direito de uso, online, antiadulteração ou integridade, bloqueios de região e travas de plataforma, modelo de hardware ou versão de firmware. O BEA adapta como um jogo roda em hardware que ele já suporta; não faz software rodar onde a editora não o lançou. |
| Aplicar patches em software do sistema | Perfis atuam nos módulos do próprio jogo, dentro da pasta do próprio usuário. |
| Baixar qualquer coisa (jogos, perfis, patches, ferramentas) | O BEA é offline. Bancos de patches da comunidade sem licença não são incluídos nem baixados. |
| Distribuir arquivos derivados de jogos | As saídas ficam com o usuário; o repositório distribui receitas (perfis), nunca resultados. |

Se o seu caso de uso precisa de um destes itens, ele precisa de outra ferramenta.
