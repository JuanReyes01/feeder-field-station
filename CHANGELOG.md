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
  una trampa pasaría de 78.
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
- Las listas de más de 26 individuos ahora sí llegan a la trampa: iban en
  partes de 27 que la gateway no podía reenviar y botaba sin avisar. Ahora van
  de a 26, y cada trampa admite hasta 78 individuos de interés.
- La lista de interés ya no se queda en "Enviando a la trampa…": cada lista
  nueva lleva un número que la trampa no ha visto, y si la trampa sigue
  reportando sin confirmarla, se reenvía con otro número, nunca antes de 60 s
  desde el primer envío (antes cambiaba cada 5 s y gastaba aire de radio). Se
  reintenta al menos cada 30 s.
- Enviar una lista ya no cancela un Armar ni deja la trampa suelta después de
  "Disparar (prueba)": la lista espera y sale cuando la trampa confirma.
- "Desactivar todas las trampas" (en Inicio y en el menú): desarma todas las
  trampas con pestillo, que quedan sueltas aunque se reinicien, hasta "Activar
  todas". Mientras tanto, una trampa que se conecte o se arme sola se desarma
  también, y no se puede armar ni probar una sola.
- Aviso de captura con sonido: una campana suena en la estación cada 6 s hasta
  que respondes la captura, con notificación del sistema y "¡Captura!" en el
  título de la pestaña. Se activa una vez con "Sonido de captura" en el menú
  (el navegador exige un clic) y queda recordado en ese computador.
- Individuos muestra primero un resumen de los que coinciden con los filtros:
  cifras, gráficas por especie, sexo, edad, localidad, proyecto y mes de la
  última captura, y cómo quedaría cada trampa al marcarlos. Un clic en una
  barra filtra por ella. La lista de individuos, las lecturas desconocidas y
  el formulario para agregar se abren solo cuando se piden.
- Visitas muestra primero cifras (visitas, individuos, lecturas, duración
  mediana, lecturas desconocidas, hora dudosa) y gráficas por hora del día, por
  día, por especie, por trampa y los individuos con más visitas, calculadas
  sobre todo el periodo. La tabla visita por visita se abre cuando se pide.
  Nuevo periodo: últimos 30 días.
- La página de cada individuo junta todos los tags que ha llevado y muestra:
  cuándo fue marcado, su última captura, cuántas recapturas tiene, la primera
  y la última vez que una trampa lo leyó, la variación de cada medida entre
  capturas (primera, última, cambio, rango, mediana, desviación estándar, CV y
  una gráfica) resaltando los valores que se alejan de su mediana más que un
  umbral que se ajusta, y cada captura con todo su registro: medidas, muda,
  horas, método, quién lo marcó y lo midió, muestra y fotos.
- La importación de la base del CICG guarda ahora todas las columnas de cada
  captura. Hay que volver a importar el Excel una vez para traer las medidas.
- En Visitas, el individuo de cada visita abre su página (también en la
  pestaña Visitas de cada trampa).
- Individuos ordena su resumen por relevancia: especie, tiempo sin recapturar,
  proyecto, localidad, y sexo y edad. "Tiempo sin recapturar" (menos de 3
  meses, 3 a 6, 6 a 12, 1 a 2 años, más de 2) reemplaza la gráfica por mes y
  también es un filtro. Las barras van de mayor a menor, con "Desconocido" y
  "Sin especie" al final. Cómo quedarían las listas de las trampas se ve junto
  al botón de marcar.
- Visitas muestra tres cifras (visitas, individuos, lecturas desconocidas) y
  una nota con las lecturas, la duración y la hora sin confirmar; las gráficas
  van por hora, por especie y los individuos con más visitas; "por día" solo
  con periodos de 7 días o más.
- Un individuo es su código CICG: los tags que ha llevado (el que lleva y los
  que perdió) cuentan como un solo individuo. Individuos muestra una fila por
  colibrí y cuenta colibríes; los filtros de proyecto, localidad y fechas miran
  todas sus capturas, con cualquier tag. Su página se abre por código
  (`#/individuo/CICG_184`) y marcarlo de interés siempre marca el tag que lleva
  ahora. Visitas, Capturas, Bitácora y el aviso de captura lo nombran por su
  código; una lectura de un tag perdido dice "tag perdido de CICG_…".
- Las exportaciones de lecturas y visitas traen `codigo_cicg` y `tag_en_uso`.
- La importación avisa cuando un código tiene dos tags en uso a la vez; la
  estación los marca los dos.
- Página Estación (abajo en el menú): gateway, sonido de captura y
  notificaciones, datos (importar la base del CICG, exportar, respaldo
  completo), reloj y qué hacer si no está confirmado, versión con "Buscar
  actualizaciones", y el umbral de las medidas.
- El menú lleva arriba el control de las trampas: "Desactivar todas" actúa al
  instante y "Activar todas" pregunta antes. El sonido de captura es un
  interruptor y el estado de la gateway y de las trampas se ve abajo.
- Botones con una sola regla: botón para hacer algo, enlace para ir a otra
  página; un solo botón verde por pantalla; rojo solo para soltar una trampa o
  quitar individuos. Pestañas en cada trampa, selector de periodo y de eventos,
  ruta "Inicio › Trampa" en vez de "Volver a…". El resultado de cada acción
  aparece abajo de la pantalla, y marcar o quitar se puede deshacer.
- La configuración de una trampa deja cambiar su tipo (con pestillo o solo
  lector) y volver a prepararla. La lista de interés de una trampa muestra cada
  colibrí una vez, con su estado, "Quitar" y un buscador para agregar.
- En el celular el menú es una barra compacta arriba, y el nombre de cada trampa
  en Inicio abre su página.
- Sexo y edad de la base del CICG se muestran en español.
- En Windows, la estación comprueba su reloj contra un servidor de hora antes
  de pasárselo a las trampas.
