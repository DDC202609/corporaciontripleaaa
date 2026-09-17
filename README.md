# Autolote Web — Corporación Triple AAA — V3

Base de desarrollo local para convertir el sistema Excel en una aplicación web.

## Incluye en esta versión
- Dashboard inicial.
- Maestro de vehículos con búsqueda y filtro por estado.
- Alta y edición de vehículos.
- Ficha individual del vehículo.
- Costos adicionales vinculados al VIN.
- Cálculo automático de costo real, utilidad y margen.
- Vista de venta cuando exista una venta registrada.
- Catálogo contable importado del desarrollo anterior.
- Áreas y departamentos.
- Módulo inicial de comisiones.

## Ejecutar
1. Instalar Python 3.10+.
2. Abrir terminal en esta carpeta.
3. `pip install -r requirements.txt`
4. `python server.py`
5. Abrir `http://127.0.0.1:5000`

La aplicación incluye autenticación local y está preparada para un despliegue público de una sola instancia. Mantenga copias periódicas de la base de datos antes de realizar cambios masivos.

## Despliegue en Render

El proyecto incluye `render.yaml` para desplegarlo como servicio web en Render.

- El primer arranque copia `data/autolote.sqlite` a `/var/data/autolote.sqlite`.
- El disco persistente conserva la operación y los usuarios en cada despliegue.
- Configure el repositorio como privado: la base inicial contiene información operativa.
- Después del primer despliegue, agregue `corporaciontripleaaa.com` y `www.corporaciontripleaaa.com` en Render. En Cloudflare cree ambos CNAME hacia la dirección `onrender.com` asignada por Render, inicialmente con **DNS only**. Cuando Render emita el certificado, puede habilitar el proxy de Cloudflare.
