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
- Biomark en toda la interfaz y en las exportaciones.
- Individuos: importar la lista del CICG (Excel o CSV), agregar a mano,
  marcar de interés en todas las trampas o en algunas. Los tags que no son de
  nadie quedan como "lectura desconocida".
- Visitas agrupadas como en feeder-analysis (3 s sin leer cierran la visita)
  y exportación de lecturas y visitas a CSV.
- Las trampas aparecen solas al prenderse, con el nombre que sale de su MAC.
- La trampa queda siempre armada: "Disparar (prueba)" suelta y rearma sin
  registrar captura; "Desarmar para mantenimiento" la deja suelta.
- Al responder una captura ("Capturado" o "Sin colibrí") la trampa vuelve a
  aceptar ese mismo tag.
- En Windows, la estación comprueba su reloj contra un servidor de hora antes
  de pasárselo a las trampas.
