# Cambios

Cada versión de la estación, en español, para quien la usa en campo. Las notas
de cada release en GitHub salen de aquí (packaging/release_notes.py): la
sección se llama con el número de versión, sin la "v" y sin "-rc".

## 0.2.0

- La estación se instala como una aplicación: instalador para Windows (menú
  Inicio, escritorio, desinstalador) y para Linux (menú de aplicaciones), o
  versión portable que se abre sin instalar.
- Avisa cuando hay una versión nueva. La instalada se actualiza con un clic;
  la portable ofrece descargar el archivo nuevo. Los datos se quedan.
- Pantalla Gateway: elegir el puerto de la gateway, que se prueba al momento y
  se recuerda. Sin elegir, usa el único puerto USB que haya.
- Interfaz nueva: Inicio, cada trampa (preparar, control, configuración,
  lista de interés, visitas, bitácora), individuos, visitas, capturas y
  bitácora en vivo.
- Biomark en toda la interfaz y en las exportaciones, con 12 cifras después
  del guion bajo como en la base del CICG: los tags 901 se ven como
  `901_021000242715`, no `901_21000242715`.
- Individuos: importar la lista del CICG (Excel o CSV), agregar a mano,
  marcar de interés en todas las trampas o en algunas. Los tags que no son de
  nadie quedan como "lectura desconocida".
- Marcar de interés por filtros: especie, día de captura (marcaje o
  recaptura, de la lista del CICG), estado o búsqueda; o pegando varios
  biomark o anillos. Se marca en todas las trampas o en una, avisando antes si
  una trampa pasaría de 81.
- Cada individuo muestra si su trampa ya lo tiene, si se le está enviando o si
  lo recibe al conectarse. Una lectura desconocida se marca de interés en un
  paso.
- Importa la base de individuos del CICG (CICG_Individuals_database.xlsx): toma
  la hoja de capturas, el código CICG, el anillo (Ring), proyecto y localidad de
  cada captura, y cuenta las recapturas escritas sin PIT. Marca los tags que el
  colibrí perdió (Other code o un PIT nuevo con el mismo código) para no gastar
  cupo de la trampa en ellos.
- Filtros nuevos: sexo, edad, proyecto, localidad y rango de fechas de captura;
  se puede marcar escribiendo el código CICG.
- Visitas agrupadas como en feeder-analysis (3 s sin leer cierran la visita)
  y exportación de lecturas y visitas a CSV.
- Las trampas aparecen solas al prenderse, con el nombre que sale de su MAC.
- La trampa queda siempre armada: "Disparar (prueba)" suelta y rearma sin
  registrar captura; "Desarmar para mantenimiento" la deja suelta.
- Al responder una captura ("Capturado" o "Sin colibrí") la trampa vuelve a
  aceptar ese mismo tag.
- La lista de interés ya no se queda en "Enviando a la trampa…": cada lista
  nueva lleva un número que la trampa no ha visto, y si la trampa sigue
  reportando sin confirmarla, se reenvía con otro número. Se reintenta al
  menos cada 30 s.
- Enviar una lista ya no cancela un Armar ni deja la trampa suelta después de
  "Disparar (prueba)": la lista espera y sale cuando la trampa confirma.
- En Windows, la estación comprueba su reloj contra un servidor de hora antes
  de pasárselo a las trampas.
