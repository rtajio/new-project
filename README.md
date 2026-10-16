# Buscador de pasajes Tacna → Lima

Script en Python con Playwright que consulta varias fuentes, simula distintas formas de entrar (PC o móvil; Perú, EE. UU. o Chile; soles, dólares o pesos chilenos), convierte todo a soles con el tipo de cambio del día y guarda cada observación en una base SQLite. Así se puede ver con datos, y no por impresión, si el precio cambia según el dispositivo, el mercado o la hora.

## Por qué cambia el precio

1. **Precios dinámicos.** Las aerolíneas venden por tramos de tarifa (clases de reserva). Cuando se agotan los asientos de un tramo, el precio sube; además, a veces liberan cupos promocionales y el precio vuelve a bajar.
2. **Mercado y moneda.** El mismo vuelo puede costar distinto según el país desde el que se consulta y la moneda en la que se cobra.
3. **Tipo de cambio mostrado.** SKY cobra en USD y muestra un equivalente en soles. En la prueba del 16/10/2026, el mismo vuelo de USD 65.14 aparecía como S/ 226.12 en PC y S/ 239.72 en móvil.
4. **Caché e historial.** Los metabuscadores (Google Flights, Kayak, etc.) actualizan con retraso: Google advierte que sus datos pueden tener hasta 24 h de antigüedad. Por eso aquí se consulta también el sitio oficial.

## Fuentes

| Fuente | Qué cubre | Perfiles |
|---|---|---|
| Google Flights | LATAM (y otras aerolíneas que publiquen tarifa) | PC Perú PEN, móvil iPhone Perú PEN, PC EE. UU. USD, Android Chile CLP |
| SKY Airline (sitio oficial) | Vuelos SKY y calendario de tarifa mínima por día (±2 semanas) | PC Perú, móvil Android Perú |

LATAM, Kayak y Skyscanner bloquean el acceso automatizado (página "Access Denied" o captcha), así que LATAM se consulta a través de Google Flights. JetSMART no aparece porque Tacna (TCQ) no figura entre sus estaciones de venta; su buscador responde "No results found" (verificado el 16/10/2026).

## Instalación

```bash
pip install -r requirements.txt
playwright install chromium        # solo la primera vez, en tu PC
```

## Uso

```bash
python buscar_pasajes.py                          # 6-nov-2026, todas las fuentes
python buscar_pasajes.py --flex 2                 # además ±2 días (4 al 8 de noviembre)
python buscar_pasajes.py --cada 180 --rondas 8    # repetir cada 3 h, 8 veces (≈ 1 día)
python buscar_pasajes.py --historial              # mín., mediana, máx. y DE por fuente/perfil
python buscar_pasajes.py --csv precios.csv        # exportar todas las observaciones
python buscar_pasajes.py --ver                    # ver el navegador mientras trabaja
python buscar_pasajes.py --origen LIM --destino TCQ --fecha 2026-11-20   # otra ruta o fecha
```

La salida muestra: (a) el ranking de tarifas en soles, (b) los vuelos que cuestan distinto según el perfil (diferencia de S/ 3 o más), (c) el calendario de tarifa mínima de SKY y (d) el mejor precio con su enlace directo.

## Resultado de la primera corrida (16/10/2026, 02:07 h)

| Vuelo 6-nov | Hora | Precio | Fuente |
|---|---|---|---|
| SKY | 08:45 y 17:20 | USD 65.14 (≈ S/ 224–240) | sitio oficial, "Tarifa promocional", quedaban 6 y 4 asientos |
| LATAM | 09:40 | S/ 233 (PEN) · USD 68 · CLP 66 258 (≈ S/ 231) | Google Flights |
| LATAM | 15:40 | S/ 245 | Google Flights |

Son precios por adulto, con tasas incluidas y sin equipaje en bodega. Para SKY, el 1, 2, 3, 4, 5 y 6 de noviembre tienen la misma tarifa mínima (USD 65.14); desde el 8 de noviembre sube a USD 94.64.

## Limitaciones

- Los sitios cambian su diseño; si una fuente deja de devolver tarifas, el script lo informa y continúa con las demás.
- El precio final depende del tipo de tarifa (Basic, Light, Plus…), del equipaje y del medio de pago. Antes de pagar, revisa siempre el total en la última pantalla.
- Úsalo con moderación (pocas consultas por hora). Esto respeta los sitios y evita bloqueos.
