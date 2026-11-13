from pathlib import Path
from datetime import datetime, timezone, timedelta
from reportlab.lib.enums import TA_LEFT
from reportlab.platypus import LongTable, Image
from datetime import datetime
from zoneinfo import ZoneInfo
from io import BytesIO
from xml.sax.saxutils import escape

from flask import send_file

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle
)
from flask import (
    Flask, render_template, request,
    redirect, url_for, session, flash, jsonify
)
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)

from functools import wraps
from decimal import Decimal, InvalidOperation

from db import obtener_conexion
import os
from dotenv import load_dotenv
from asistente_ia import generar_respuesta
from consultas_tienda import obtener_contexto_tienda

load_dotenv()


# ==========================================
# CONFIGURACIÓN DE FLASK
# ==========================================

app = Flask(__name__)

app.secret_key = os.getenv("FLASK_SECRET_KEY")

if not app.secret_key:
    raise RuntimeError(
        "Falta configurar FLASK_SECRET_KEY en el archivo .env"
    )

csrf = CSRFProtect(app)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")



# ==========================================
# PROTECCIÓN DE ADMINISTRADORES
# ==========================================

def administrador_requerido(funcion):

    @wraps(funcion)
    def decorada(*args, **kwargs):

        if not session.get("usuario_id"):
            flash("Debes iniciar sesión para continuar.")
            return redirect(url_for("login"))

        if session.get("usuario_perfil") != "administrador":
            flash("No tienes permisos para acceder.")
            return redirect(url_for("inicio"))

        return funcion(*args, **kwargs)

    return decorada

# ==========================================
# GESTIÓN DE INVENTARIO
# ==========================================

@app.route("/admin/inventario")
@administrador_requerido
def admin_inventario():

    conexion = None
    cursor = None

    try:
        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                p.id_producto,
                p.nombre,
                p.descripcion,
                p.precio,
                p.existencia,
                p.imagen,
                p.activo,
                p.id_categoria,
                c.nombre AS categoria
            FROM productos AS p
            LEFT JOIN categorias AS c
                ON p.id_categoria = c.id_categoria
            ORDER BY p.id_producto DESC
        """)

        productos = cursor.fetchall()

        return render_template(
            "admin_inventario.html",
            productos=productos
        )

    except Exception as error:

        print("ERROR AL CARGAR INVENTARIO:", error)
        return "No se pudo cargar el inventario.", 500

    finally:

        if cursor is not None:
            cursor.close()

        if conexion is not None and conexion.is_connected():
            conexion.close()


# ==========================================
# REGISTRAR PRODUCTO
# ==========================================

@app.route("/admin/producto/nuevo", methods=["GET", "POST"])
@administrador_requerido
def admin_nuevo_producto():

    conexion = None
    cursor = None

    try:
        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        cursor.execute("""
            SELECT id_categoria, nombre
            FROM categorias
            ORDER BY nombre
        """)

        categorias = cursor.fetchall()

        if not categorias:
            flash("Primero debes registrar una categoría.")
            return redirect(url_for("admin_inventario"))

        if request.method == "POST":

            nombre = request.form.get("nombre", "").strip()
            descripcion = request.form.get("descripcion", "").strip()
            imagen = request.form.get("imagen", "").strip()

            costo_texto = request.form.get(
                "costo_compra", ""
            ).strip()

            try:
                id_categoria = int(request.form.get("id_categoria", ""))
                precio = Decimal(request.form.get("precio", ""))
                existencia = int(request.form.get("existencia", ""))

                # El costo puede quedar vacío temporalmente.
                costo_compra = (
                    Decimal(costo_texto)
                    if costo_texto
                    else None
                )

            except (ValueError, InvalidOperation):
                flash("Introduce valores válidos para el producto.")

                return render_template(
                    "admin_producto_form.html",
                    producto=None,
                    categorias=categorias
                )

            if not nombre:
                flash("El nombre del producto es obligatorio.")

            elif len(nombre) > 150:
                flash("El nombre es demasiado largo.")

            elif len(descripcion) > 1000:
                flash("La descripción es demasiado larga.")

            elif len(imagen) > 1000:
                flash("La dirección de imagen es demasiado larga.")

            elif precio < 0 or precio > Decimal("99999999.99"):
                flash("El precio debe estar entre 0 y 99,999,999.99.")

            elif (
                costo_compra is not None
                and (
                    costo_compra < 0
                    or costo_compra > Decimal("99999999.99")
                    or costo_compra.as_tuple().exponent < -2
                )
            ):
                flash(
                    "El costo de compra debe ser válido, "
                    "positivo y tener máximo dos decimales."
                )

            elif existencia < 0:
                flash("Las existencias no pueden ser negativas.")

            elif id_categoria not in [
                categoria["id_categoria"] for categoria in categorias
            ]:
                flash("Selecciona una categoría válida.")

            else:
                # El producto y su existencia inicial se guardan
                # dentro de una misma transacción.
                conexion.start_transaction()

                cursor.execute("""
                    INSERT INTO productos (
                        id_categoria,
                        nombre,
                        descripcion,
                        precio,
                        costo_compra,
                        existencia,
                        imagen,
                        activo
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
                """, (
                    id_categoria,
                    nombre,
                    descripcion,
                    precio,
                    costo_compra,
                    existencia,
                    imagen or None
                ))

                id_producto = cursor.lastrowid

                # Registrar la existencia inicial como una entrada.
                if existencia > 0:
                    cursor.execute("""
                        INSERT INTO historial_inventario (
                            id_producto,
                            id_usuario,
                            tipo_movimiento,
                            cantidad,
                            existencia_anterior,
                            existencia_nueva,
                            motivo
                        )
                        VALUES (%s, %s, 'entrada', %s, 0, %s, %s)
                    """, (
                        id_producto,
                        session["usuario_id"],
                        existencia,
                        existencia,
                        "Existencia inicial al registrar producto"
                    ))

                conexion.commit()

                flash("Producto registrado correctamente.")
                return redirect(url_for("admin_inventario"))

        return render_template(
            "admin_producto_form.html",
            producto=None,
            categorias=categorias
        )

    except Exception as error:

        if conexion is not None:
            conexion.rollback()

        print("ERROR AL REGISTRAR PRODUCTO:", error)
        flash("No se pudo registrar el producto.")

        return redirect(url_for("admin_inventario"))

    finally:

        if cursor is not None:
            cursor.close()

        if conexion is not None and conexion.is_connected():
            conexion.close()


# ==========================================
# MODIFICAR PRODUCTO
# ==========================================


@app.route(
    "/admin/producto/<int:id_producto>/editar",
    methods=["GET", "POST"]
)
@administrador_requerido
def admin_editar_producto(id_producto):

    conexion = None
    cursor = None

    try:
        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        cursor.execute("""
            SELECT *
            FROM productos
            WHERE id_producto = %s
        """, (id_producto,))

        producto = cursor.fetchone()

        if not producto:
            flash("El producto no existe.")
            return redirect(url_for("admin_inventario"))

        cursor.execute("""
            SELECT id_categoria, nombre
            FROM categorias
            ORDER BY nombre
        """)

        categorias = cursor.fetchall()

        if request.method == "POST":

            nombre = request.form.get("nombre", "").strip()
            descripcion = request.form.get("descripcion", "").strip()
            imagen = request.form.get("imagen", "").strip()

            costo_texto = request.form.get(
                "costo_compra", ""
            ).strip()

            try:
                id_categoria = int(request.form.get("id_categoria", ""))
                precio = Decimal(request.form.get("precio", ""))

                costo_compra = (
                    Decimal(costo_texto)
                    if costo_texto
                    else None
                )

            except (ValueError, InvalidOperation):
                flash("Introduce valores válidos para el producto.")

                return render_template(
                    "admin_producto_form.html",
                    producto=producto,
                    categorias=categorias
                )

            if not nombre:
                flash("El nombre es obligatorio.")

            elif len(nombre) > 150:
                flash("El nombre es demasiado largo.")

            elif len(descripcion) > 1000:
                flash("La descripción es demasiado larga.")

            elif len(imagen) > 1000:
                flash("La dirección de imagen es demasiado larga.")

            elif (
                precio < 0
                or precio > Decimal("99999999.99")
            ):
                flash("El precio debe estar entre 0 y 99,999,999.99.")

            elif (
                costo_compra is not None
                and (
                    costo_compra < 0
                    or costo_compra > Decimal("99999999.99")
                    or costo_compra.as_tuple().exponent < -2
                )
            ):
                flash(
                    "El costo de compra debe ser válido, "
                    "positivo y tener máximo dos decimales."
                )

            elif id_categoria not in [
                categoria["id_categoria"] for categoria in categorias
            ]:
                flash("Selecciona una categoría válida.")

            else:

                cursor.execute("""
                    UPDATE productos
                    SET
                        id_categoria = %s,
                        nombre = %s,
                        descripcion = %s,
                        precio = %s,
                        costo_compra = %s,
                        imagen = %s
                    WHERE id_producto = %s
                """, (
                    id_categoria,
                    nombre,
                    descripcion,
                    precio,
                    costo_compra,
                    imagen or None,
                    id_producto
                ))

                conexion.commit()

                flash("Producto modificado correctamente.")

                return redirect(url_for("admin_inventario"))

        return render_template(
            "admin_producto_form.html",
            producto=producto,
            categorias=categorias
        )

    except Exception as error:

        if conexion is not None:
            conexion.rollback()

        print("ERROR AL MODIFICAR PRODUCTO:", error)
        flash("No se pudo modificar el producto.")

        return redirect(url_for("admin_inventario"))

    finally:

        if cursor is not None:
            cursor.close()

        if conexion is not None and conexion.is_connected():
            conexion.close()


# ==========================================
# AUMENTAR O DISMINUIR EXISTENCIAS
# ==========================================

@app.route(
    "/admin/producto/<int:id_producto>/existencias",
    methods=["POST"]
)
@administrador_requerido
def admin_actualizar_existencias(id_producto):

    conexion = None
    cursor = None

    try:
        cantidad = int(request.form.get("cantidad", "0"))
        operacion = request.form.get("operacion", "").strip()

        if cantidad <= 0:
            flash("La cantidad debe ser mayor a cero.")
            return redirect(url_for("admin_inventario"))

        if operacion not in ("agregar", "retirar"):
            flash("La operación seleccionada no es válida.")
            return redirect(url_for("admin_inventario"))

        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        conexion.start_transaction()

        cursor.execute("""
            SELECT existencia
            FROM productos
            WHERE id_producto = %s
            FOR UPDATE
        """, (id_producto,))

        producto = cursor.fetchone()

        if not producto:
            raise ValueError("El producto no existe.")

        existencia_actual = producto["existencia"]

        if operacion == "agregar":

            existencia_nueva = existencia_actual + cantidad
            tipo_movimiento = "entrada"
            mensaje = "Existencias aumentadas correctamente."

            cursor.execute("""
                UPDATE productos
                SET existencia = %s
                WHERE id_producto = %s
            """, (existencia_nueva, id_producto))

        else:

            if cantidad > existencia_actual:
                raise ValueError(
                    "No puedes retirar más unidades de las disponibles."
                )

            existencia_nueva = existencia_actual - cantidad
            tipo_movimiento = "salida"
            mensaje = "Existencias disminuidas correctamente."

            cursor.execute("""
                UPDATE productos
                SET existencia = %s
                WHERE id_producto = %s
                AND existencia >= %s
            """, (existencia_nueva, id_producto, cantidad))

            if cursor.rowcount != 1:
                raise ValueError("No se pudieron actualizar las existencias.")

        # Guardar el movimiento manual en el historial.
        cursor.execute("""
            INSERT INTO historial_inventario (
                id_producto,
                id_usuario,
                tipo_movimiento,
                cantidad,
                existencia_anterior,
                existencia_nueva,
                motivo
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            id_producto,
            session["usuario_id"],
            tipo_movimiento,
            cantidad,
            existencia_actual,
            existencia_nueva,
            "Ajuste manual realizado desde el panel administrativo"
        ))

        conexion.commit()
        flash(mensaje)

    except ValueError as error:

        if conexion is not None:
            conexion.rollback()

        flash(str(error))

    except Exception as error:

        if conexion is not None:
            conexion.rollback()

        print("ERROR AL ACTUALIZAR EXISTENCIAS:", error)
        flash("No se pudieron actualizar las existencias.")

    finally:

        if cursor is not None:
            cursor.close()

        if conexion is not None and conexion.is_connected():
            conexion.close()

    return redirect(url_for("admin_inventario"))


# ==========================================
# ACTIVAR O DESACTIVAR PRODUCTO
# ==========================================

@app.route(
    "/admin/producto/<int:id_producto>/estado",
    methods=["POST"]
)
@administrador_requerido
def admin_cambiar_estado_producto(id_producto):

    conexion = None
    cursor = None

    try:
        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        cursor.execute("""
            SELECT activo
            FROM productos
            WHERE id_producto = %s
        """, (id_producto,))

        producto = cursor.fetchone()

        if not producto:
            flash("El producto no existe.")
            return redirect(url_for("admin_inventario"))

        nuevo_estado = 0 if producto["activo"] else 1

        cursor.execute("""
            UPDATE productos
            SET activo = %s
            WHERE id_producto = %s
        """, (nuevo_estado, id_producto))

        conexion.commit()

        if nuevo_estado:
            flash("Producto activado correctamente.")
        else:
            flash("Producto desactivado correctamente.")

    except Exception as error:

        if conexion is not None:
            conexion.rollback()

        print("ERROR AL CAMBIAR ESTADO DEL PRODUCTO:", error)
        flash("No se pudo cambiar el estado del producto.")

    finally:

        if cursor is not None:
            cursor.close()

        if conexion is not None and conexion.is_connected():
            conexion.close()

    return redirect(url_for("admin_inventario"))



# ==========================================
# HISTORIAL DE INVENTARIO
# ==========================================

@app.route("/admin/historial")
@administrador_requerido
def admin_historial():

    conexion = None
    cursor = None

    try:
        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        busqueda = request.args.get("q", "").strip()
        tipo = request.args.get("tipo", "").strip()

        tipos_permitidos = ("entrada", "salida", "venta")

        consulta = """
            SELECT
                h.id_movimiento,
                h.id_producto,
                h.id_usuario,
                h.tipo_movimiento,
                h.cantidad,
                h.existencia_anterior,
                h.existencia_nueva,
                h.motivo,
                h.fecha_movimiento,
                COALESCE(p.nombre, 'Producto eliminado') AS producto,
                COALESCE(u.nombre, 'Usuario no disponible') AS usuario,
                COALESCE(u.perfil, 'No disponible') AS perfil
            FROM historial_inventario AS h
            LEFT JOIN productos AS p
                ON h.id_producto = p.id_producto
            LEFT JOIN usuarios AS u
                ON h.id_usuario = u.id_usuario
            WHERE 1 = 1
        """

        parametros = []

        if busqueda:
            consulta += """
                AND (
                    p.nombre LIKE %s
                    OR h.motivo LIKE %s
                    OR u.nombre LIKE %s
                )
            """
            termino = f"%{busqueda}%"
            parametros.extend([termino, termino, termino])

        if tipo in tipos_permitidos:
            consulta += " AND h.tipo_movimiento = %s"
            parametros.append(tipo)

        consulta += """
            ORDER BY h.fecha_movimiento DESC,
                     h.id_movimiento DESC
        """

        cursor.execute(consulta, tuple(parametros))
        movimientos = cursor.fetchall()

        cursor.execute("""
            SELECT
                COUNT(*) AS total_movimientos,
                COALESCE(SUM(
                    CASE WHEN tipo_movimiento = 'entrada'
                    THEN cantidad ELSE 0 END
                ), 0) AS total_entradas,
                COALESCE(SUM(
                    CASE WHEN tipo_movimiento = 'salida'
                    THEN cantidad ELSE 0 END
                ), 0) AS total_salidas,
                COALESCE(SUM(
                    CASE WHEN tipo_movimiento = 'venta'
                    THEN cantidad ELSE 0 END
                ), 0) AS total_ventas
            FROM historial_inventario
        """)
        estadisticas = cursor.fetchone()

        return render_template(
            "admin_historial.html",
            movimientos=movimientos,
            estadisticas=estadisticas,
            busqueda=busqueda,
            tipo=tipo
        )

    except Exception as error:
        print("ERROR AL CARGAR HISTORIAL:", error)
        return "No se pudo cargar el historial de inventario.", 500

    finally:
        if cursor is not None:
            cursor.close()

        if conexion is not None and conexion.is_connected():
            conexion.close()



@app.route("/admin/reportes")
@administrador_requerido
def admin_reportes():

    conexion = None
    cursor = None

    try:
        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        # ==========================================
        # FILTROS
        # ==========================================

        mes = request.args.get("mes", "").strip()
        anio = request.args.get("anio", "").strip()
        busqueda = request.args.get("q", "").strip()
        orden = request.args.get("orden", "cantidad").strip()

        if mes and (not mes.isdigit() or not 1 <= int(mes) <= 12):
            mes = ""

        if anio and (not anio.isdigit() or not 2000 <= int(anio) <= 2100):
            anio = ""

        if orden not in ("cantidad", "ingresos", "nombre"):
            orden = "cantidad"

        # ==========================================
        # CONDICIONES PARA PRODUCTOS Y DETALLE
        # ==========================================

        condiciones = [
            "p.estado IN ('pagado', 'enviado', 'entregado')"
        ]

        parametros = []

        if mes:
            condiciones.append("MONTH(p.fecha_pedido) = %s")
            parametros.append(int(mes))

        if anio:
            condiciones.append("YEAR(p.fecha_pedido) = %s")
            parametros.append(int(anio))

        if busqueda:
            condiciones.append("pr.nombre LIKE %s")
            parametros.append(f"%{busqueda}%")

        where = " AND ".join(condiciones)

        # ==========================================
        # ORDENAMIENTO
        # ==========================================

        ordenamientos = {
            "cantidad": "unidades_vendidas DESC",
            "ingresos": "ingresos DESC",
            "nombre": "producto ASC"
        }

        orden_sql = ordenamientos[orden]

        # ==========================================
        # PRODUCTOS VENDIDOS
        # ==========================================

        cursor.execute(f"""
            SELECT
                pr.id_producto,
                pr.nombre AS producto,

                COALESCE(
                    SUM(dp.cantidad),
                    0
                ) AS unidades_vendidas,

                COALESCE(
                    SUM(dp.cantidad * dp.precio_unitario),
                    0
                ) AS ingresos

            FROM detalle_pedido AS dp

            INNER JOIN pedidos AS p
                ON dp.id_pedido = p.id_pedido

            INNER JOIN productos AS pr
                ON dp.id_producto = pr.id_producto

            WHERE {where}

            GROUP BY
                pr.id_producto,
                pr.nombre

            ORDER BY {orden_sql}
        """, tuple(parametros))

        productos_vendidos = cursor.fetchall()

        # ==========================================
        # RESUMEN GENERAL
        # ==========================================

        condiciones_resumen = [
            "p.estado IN ('pagado', 'enviado', 'entregado')"
        ]

        parametros_resumen = []

        if mes:
            condiciones_resumen.append("MONTH(p.fecha_pedido) = %s")
            parametros_resumen.append(int(mes))

        if anio:
            condiciones_resumen.append("YEAR(p.fecha_pedido) = %s")
            parametros_resumen.append(int(anio))

        where_resumen = " AND ".join(condiciones_resumen)

        cursor.execute(f"""
            SELECT
                COUNT(DISTINCT p.id_pedido) AS total_pedidos,

                COALESCE(
                    SUM(dp.cantidad),
                    0
                ) AS unidades_vendidas,

                COALESCE(
                    SUM(dp.cantidad * dp.precio_unitario),
                    0
                ) AS ingresos,

                CASE
                    WHEN SUM(
                        CASE
                            WHEN dp.id_detalle IS NOT NULL
                                 AND dp.costo_unitario IS NULL
                            THEN 1
                            ELSE 0
                        END
                    ) > 0
                    THEN NULL

                    ELSE COALESCE(
                        SUM(
                            dp.cantidad *
                            (dp.precio_unitario - dp.costo_unitario)
                        ),
                        0
                    )
                END AS ganancias,

                COALESCE(
                    SUM(
                        CASE
                            WHEN dp.id_detalle IS NOT NULL
                                 AND dp.costo_unitario IS NULL
                            THEN 1
                            ELSE 0
                        END
                    ),
                    0
                ) AS ventas_sin_costo

            FROM pedidos AS p

            LEFT JOIN detalle_pedido AS dp
                ON p.id_pedido = dp.id_pedido

            WHERE {where_resumen}
        """, tuple(parametros_resumen))

        resumen = cursor.fetchone()

        # ==========================================
        # INGRESOS Y GANANCIAS MENSUALES
        # ==========================================
        # Esta consulta respeta el año y la búsqueda
        # del producto, pero muestra todos los meses.
        # El filtro de mes no se aplica aquí.

        condiciones_mensuales = [
            "p.estado IN ('pagado', 'enviado', 'entregado')"
        ]

        parametros_mensuales = []

        if anio:
            condiciones_mensuales.append("YEAR(p.fecha_pedido) = %s")
            parametros_mensuales.append(int(anio))

        if busqueda:
            condiciones_mensuales.append("pr.nombre LIKE %s")
            parametros_mensuales.append(f"%{busqueda}%")

        where_mensual = " AND ".join(condiciones_mensuales)

        cursor.execute(f"""
            SELECT
                YEAR(p.fecha_pedido) AS anio,
                MONTH(p.fecha_pedido) AS numero_mes,

                COALESCE(
                    SUM(dp.cantidad * dp.precio_unitario),
                    0
                ) AS ingresos,

                CASE
                    WHEN SUM(
                        CASE
                            WHEN dp.costo_unitario IS NULL THEN 1
                            ELSE 0
                        END
                    ) > 0
                    THEN NULL

                    ELSE COALESCE(
                        SUM(
                            dp.cantidad *
                            (dp.precio_unitario - dp.costo_unitario)
                        ),
                        0
                    )
                END AS ganancias,

                SUM(
                    CASE
                        WHEN dp.costo_unitario IS NULL THEN 1
                        ELSE 0
                    END
                ) AS ventas_sin_costo

            FROM detalle_pedido AS dp

            INNER JOIN pedidos AS p
                ON dp.id_pedido = p.id_pedido

            INNER JOIN productos AS pr
                ON dp.id_producto = pr.id_producto

            WHERE {where_mensual}

            GROUP BY
                YEAR(p.fecha_pedido),
                MONTH(p.fecha_pedido)

            ORDER BY
                anio ASC,
                numero_mes ASC
        """, tuple(parametros_mensuales))

        ventas_mensuales = cursor.fetchall()

        # ==========================================
        # DETALLE DE VENTAS Y COMPRADORES
        # ==========================================

        cursor.execute(f"""
            SELECT
                p.id_pedido,
                p.fecha_pedido,
                u.nombre AS comprador,
                pr.nombre AS producto,
                dp.cantidad,
                dp.precio_unitario,
                dp.costo_unitario,

                (
                    dp.cantidad * dp.precio_unitario
                ) AS subtotal,

                CASE
                    WHEN dp.costo_unitario IS NULL
                    THEN NULL

                    ELSE (
                        dp.cantidad *
                        (dp.precio_unitario - dp.costo_unitario)
                    )
                END AS ganancia

            FROM detalle_pedido AS dp

            INNER JOIN pedidos AS p
                ON dp.id_pedido = p.id_pedido

            INNER JOIN productos AS pr
                ON dp.id_producto = pr.id_producto

            LEFT JOIN usuarios AS u
                ON p.id_usuario = u.id_usuario

            WHERE {where}

            ORDER BY
                p.fecha_pedido DESC,
                p.id_pedido DESC
        """, tuple(parametros))

        detalle_ventas = cursor.fetchall()

        # ==========================================
        # AÑOS DISPONIBLES
        # ==========================================

        cursor.execute("""
            SELECT DISTINCT YEAR(fecha_pedido) AS anio
            FROM pedidos
            WHERE fecha_pedido IS NOT NULL
            ORDER BY anio DESC
        """)

        anios_disponibles = [
            fila["anio"]
            for fila in cursor.fetchall()
            if fila["anio"] is not None
        ]

        # ==========================================
        # ENVIAR INFORMACIÓN A LA PLANTILLA
        # ==========================================

        return render_template(
            "admin_reportes.html",
            productos_vendidos=productos_vendidos,
            resumen=resumen,
            ventas_mensuales=ventas_mensuales,
            detalle_ventas=detalle_ventas,
            mes=mes,
            anio=anio,
            busqueda=busqueda,
            orden=orden,
            anios_disponibles=anios_disponibles
        )

    except Exception as error:

        print("ERROR AL GENERAR REPORTES:", error)
        return "No se pudieron generar los reportes.", 500

    finally:

        if cursor is not None:
            cursor.close()

        if conexion is not None and conexion.is_connected():
            conexion.close()


@app.route("/admin/reportes/pdf")
@administrador_requerido
def admin_reportes_pdf():

    conexion = None
    cursor = None

    try:
        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        # ==========================================
        # FILTROS
        # ==========================================

        mes = request.args.get("mes", "").strip()
        anio = request.args.get("anio", "").strip()
        busqueda = request.args.get("q", "").strip()
        orden = request.args.get("orden", "cantidad").strip()

        if mes and (not mes.isdigit() or not 1 <= int(mes) <= 12):
            mes = ""

        if anio and (not anio.isdigit() or not 2000 <= int(anio) <= 2100):
            anio = ""

        if orden not in ("cantidad", "ingresos", "nombre"):
            orden = "cantidad"

        nombres_meses = [
            "",
            "Enero", "Febrero", "Marzo", "Abril",
            "Mayo", "Junio", "Julio", "Agosto",
            "Septiembre", "Octubre", "Noviembre", "Diciembre"
        ]

        # ==========================================
        # CONDICIONES GENERALES
        # ==========================================

        condiciones = [
            "p.estado IN ('pagado', 'enviado', 'entregado')"
        ]

        parametros = []

        if mes:
            condiciones.append("MONTH(p.fecha_pedido) = %s")
            parametros.append(int(mes))

        if anio:
            condiciones.append("YEAR(p.fecha_pedido) = %s")
            parametros.append(int(anio))

        if busqueda:
            condiciones.append("pr.nombre LIKE %s")
            parametros.append(f"%{busqueda}%")

        where = " AND ".join(condiciones)

        # ==========================================
        # ORDENAMIENTO
        # ==========================================

        ordenamientos = {
            "cantidad": "unidades_vendidas DESC",
            "ingresos": "ingresos DESC",
            "nombre": "producto ASC"
        }

        orden_sql = ordenamientos[orden]

        # ==========================================
        # PRODUCTOS VENDIDOS
        # ==========================================

        cursor.execute(f"""
            SELECT
                pr.id_producto,
                pr.nombre AS producto,

                COALESCE(
                    SUM(dp.cantidad),
                    0
                ) AS unidades_vendidas,

                COALESCE(
                    SUM(dp.cantidad * dp.precio_unitario),
                    0
                ) AS ingresos

            FROM detalle_pedido AS dp

            INNER JOIN pedidos AS p
                ON dp.id_pedido = p.id_pedido

            INNER JOIN productos AS pr
                ON dp.id_producto = pr.id_producto

            WHERE {where}

            GROUP BY
                pr.id_producto,
                pr.nombre

            ORDER BY {orden_sql}
        """, tuple(parametros))

        productos_vendidos = cursor.fetchall()

        # ==========================================
        # RESUMEN GENERAL
        # ==========================================

        condiciones_resumen = [
            "p.estado IN ('pagado', 'enviado', 'entregado')"
        ]

        parametros_resumen = []

        if mes:
            condiciones_resumen.append("MONTH(p.fecha_pedido) = %s")
            parametros_resumen.append(int(mes))

        if anio:
            condiciones_resumen.append("YEAR(p.fecha_pedido) = %s")
            parametros_resumen.append(int(anio))

        where_resumen = " AND ".join(condiciones_resumen)

        cursor.execute(f"""
            SELECT
                COUNT(DISTINCT p.id_pedido) AS total_pedidos,

                COALESCE(
                    SUM(dp.cantidad),
                    0
                ) AS unidades_vendidas,

                COALESCE(
                    SUM(dp.cantidad * dp.precio_unitario),
                    0
                ) AS ingresos,

                CASE
                    WHEN SUM(
                        CASE
                            WHEN dp.id_detalle IS NOT NULL
                                 AND dp.costo_unitario IS NULL
                            THEN 1
                            ELSE 0
                        END
                    ) > 0
                    THEN NULL

                    ELSE COALESCE(
                        SUM(
                            dp.cantidad *
                            (dp.precio_unitario - dp.costo_unitario)
                        ),
                        0
                    )
                END AS ganancias,

                COALESCE(
                    SUM(
                        CASE
                            WHEN dp.id_detalle IS NOT NULL
                                 AND dp.costo_unitario IS NULL
                            THEN 1
                            ELSE 0
                        END
                    ),
                    0
                ) AS ventas_sin_costo

            FROM pedidos AS p

            LEFT JOIN detalle_pedido AS dp
                ON p.id_pedido = dp.id_pedido

            WHERE {where_resumen}
        """, tuple(parametros_resumen))

        resumen = cursor.fetchone()

        # ==========================================
        # INGRESOS Y GANANCIAS MENSUALES
        # ==========================================

        condiciones_mensuales = [
            "p.estado IN ('pagado', 'enviado', 'entregado')"
        ]

        parametros_mensuales = []

        if anio:
            condiciones_mensuales.append(
                "YEAR(p.fecha_pedido) = %s"
            )
            parametros_mensuales.append(int(anio))

        if busqueda:
            condiciones_mensuales.append("pr.nombre LIKE %s")
            parametros_mensuales.append(f"%{busqueda}%")

        where_mensual = " AND ".join(condiciones_mensuales)

        cursor.execute(f"""
            SELECT
                YEAR(p.fecha_pedido) AS anio,
                MONTH(p.fecha_pedido) AS numero_mes,

                COALESCE(
                    SUM(dp.cantidad * dp.precio_unitario),
                    0
                ) AS ingresos,

                CASE
                    WHEN SUM(
                        CASE
                            WHEN dp.costo_unitario IS NULL THEN 1
                            ELSE 0
                        END
                    ) > 0
                    THEN NULL

                    ELSE COALESCE(
                        SUM(
                            dp.cantidad *
                            (dp.precio_unitario - dp.costo_unitario)
                        ),
                        0
                    )
                END AS ganancias,

                SUM(
                    CASE
                        WHEN dp.costo_unitario IS NULL THEN 1
                        ELSE 0
                    END
                ) AS ventas_sin_costo

            FROM detalle_pedido AS dp

            INNER JOIN pedidos AS p
                ON dp.id_pedido = p.id_pedido

            INNER JOIN productos AS pr
                ON dp.id_producto = pr.id_producto

            WHERE {where_mensual}

            GROUP BY
                YEAR(p.fecha_pedido),
                MONTH(p.fecha_pedido)

            ORDER BY
                anio ASC,
                numero_mes ASC
        """, tuple(parametros_mensuales))

        ventas_mensuales = cursor.fetchall()

        # ==========================================
        # DETALLE DE VENTAS
        # ==========================================

        cursor.execute(f"""
            SELECT
                p.id_pedido,
                p.fecha_pedido,
                u.nombre AS comprador,
                pr.nombre AS producto,
                dp.cantidad,
                dp.precio_unitario,
                dp.costo_unitario,

                (
                    dp.cantidad * dp.precio_unitario
                ) AS subtotal,

                CASE
                    WHEN dp.costo_unitario IS NULL
                    THEN NULL

                    ELSE (
                        dp.cantidad *
                        (dp.precio_unitario - dp.costo_unitario)
                    )
                END AS ganancia

            FROM detalle_pedido AS dp

            INNER JOIN pedidos AS p
                ON dp.id_pedido = p.id_pedido

            INNER JOIN productos AS pr
                ON dp.id_producto = pr.id_producto

            LEFT JOIN usuarios AS u
                ON p.id_usuario = u.id_usuario

            WHERE {where}

            ORDER BY
                p.fecha_pedido DESC,
                p.id_pedido DESC
        """, tuple(parametros))

        detalle_ventas = cursor.fetchall()

        # ==========================================
        # PREPARAR DOCUMENTO PDF
        # ==========================================

        buffer = BytesIO()

        documento = SimpleDocTemplate(
            buffer,
            pagesize=landscape(A4),
            rightMargin=12 * mm,
            leftMargin=12 * mm,
            topMargin=15 * mm,
            bottomMargin=17 * mm,
            title="Reporte de ventas - MUNDO INFORMÁTICO",
            author="MUNDO INFORMÁTICO"
        )

        estilos = getSampleStyleSheet()

        estilo_titulo = ParagraphStyle(
            "TituloTienda",
            parent=estilos["Title"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=24,
            textColor=colors.HexColor("#0284c7"),
            alignment=TA_LEFT,
            spaceAfter=5
        )

        estilo_subtitulo = ParagraphStyle(
            "SubtituloTienda",
            parent=estilos["Normal"],
            fontSize=10,
            leading=14,
            textColor=colors.HexColor("#475569")
        )

        estilo_seccion = ParagraphStyle(
            "TituloSeccion",
            parent=estilos["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=13,
            leading=17,
            textColor=colors.HexColor("#0369a1"),
            spaceBefore=12,
            spaceAfter=8
        )

        estilo_normal = ParagraphStyle(
            "TextoNormal",
            parent=estilos["Normal"],
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#1e293b")
        )

        estilo_tabla = ParagraphStyle(
            "TextoTabla",
            parent=estilos["Normal"],
            fontSize=7,
            leading=9,
            textColor=colors.HexColor("#1e293b")
        )

        estilo_tabla_blanco = ParagraphStyle(
            "TextoTablaBlanco",
            parent=estilos["Normal"],
            fontSize=7,
            leading=9,
            textColor=colors.white
        )

        contenido = []

        # ==========================================
        # LOGO
        # ==========================================

        carpeta_logo = Path(app.root_path) / "static" / "img"

        archivos_logo = []

        if carpeta_logo.exists():

            archivos_logo = (
                list(carpeta_logo.glob("*.jpeg")) +
                list(carpeta_logo.glob("*.jpg")) +
                list(carpeta_logo.glob("*.JPEG")) +
                list(carpeta_logo.glob("*.JPG"))
            )

        logo_path = None

        if archivos_logo:

            logos_con_nombre = [
                archivo
                for archivo in archivos_logo
                if "logo" in archivo.stem.lower()
            ]

            if logos_con_nombre:
                logo_path = logos_con_nombre[0]

            elif len(archivos_logo) == 1:
                logo_path = archivos_logo[0]

        # ==========================================
        # ENCABEZADO DEL PDF
        # ==========================================

        zona_mexico = timezone(timedelta(hours=-6))
        fecha_generacion = datetime.now(zona_mexico)

        nombre_admin = session.get(
            "usuario_nombre",
            "Administrador"
        )

        fecha_texto = fecha_generacion.strftime(
            "%d/%m/%Y %H:%M"
        )

        if mes and anio:
            periodo = f"{nombres_meses[int(mes)]} de {anio}"

        elif mes:
            periodo = nombres_meses[int(mes)]

        elif anio:
            periodo = f"Todos los meses de {anio}"

        else:
            periodo = "Todos los periodos disponibles"

        if busqueda:
            periodo += f" | Producto: {busqueda}"

        encabezado_texto = [
            Paragraph(
                "MUNDO INFORMÁTICO",
                estilo_titulo
            ),
            Paragraph(
                "REPORTE DE VENTAS",
                estilo_subtitulo
            ),
            Spacer(1, 5),
            Paragraph(
                f"<b>Periodo:</b> {escape(periodo)}",
                estilo_normal
            ),
            Paragraph(
                f"<b>Administrador:</b> {escape(str(nombre_admin))}",
                estilo_normal
            ),
            Paragraph(
                f"<b>Generado:</b> {fecha_texto}",
                estilo_normal
            )
        ]

        if logo_path:

            imagen_logo = Image(
                str(logo_path),
                width=65,
                height=65
            )

            encabezado = Table(
                [[imagen_logo, encabezado_texto]],
                colWidths=[75, 650]
            )

        else:

            encabezado = Table(
                [[encabezado_texto]],
                colWidths=[725]
            )

        encabezado.setStyle(
            TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                ("LINEBELOW", (0, 0), (-1, -1), 1, colors.HexColor("#38bdf8"))
            ])
        )

        contenido.append(encabezado)
        contenido.append(Spacer(1, 15))

        # ==========================================
        # RESUMEN GENERAL
        # ==========================================

        contenido.append(
            Paragraph("RESUMEN DE VENTAS", estilo_seccion)
        )

        ganancias_resumen = (
            f"${resumen['ganancias']:,.2f}"
            if resumen["ganancias"] is not None
            else "No disponibles"
        )

        datos_resumen = [
            ["Pedidos realizados", "Unidades vendidas", "Ingresos", "Ganancias"],
            [
                str(resumen["total_pedidos"]),
                str(resumen["unidades_vendidas"]),
                f"${resumen['ingresos']:,.2f}",
                ganancias_resumen
            ]
        ]

        tabla_resumen = Table(
            datos_resumen,
            colWidths=[180, 180, 180, 180]
        )

        tabla_resumen.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#075985")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#e0f2fe")),
                ("TEXTCOLOR", (0, 1), (-1, 1), colors.HexColor("#0f172a")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#94a3b8")),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#94a3b8")),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10)
            ])
        )

        contenido.append(tabla_resumen)

        if resumen["ventas_sin_costo"] > 0:

            contenido.append(Spacer(1, 7))

            contenido.append(
                Paragraph(
                    "Nota: existen "
                    f"{resumen['ventas_sin_costo']} registros de productos "
                    "vendidos sin costo de adquisición. Por ello, la "
                    "ganancia total no está disponible.",
                    estilo_normal
                )
            )

        # ==========================================
        # PRODUCTOS VENDIDOS
        # ==========================================

        contenido.append(
            Paragraph("PRODUCTOS VENDIDOS", estilo_seccion)
        )

        if productos_vendidos:

            datos_productos = [[
                "Producto",
                "Unidades vendidas",
                "Ingresos"
            ]]

            for producto in productos_vendidos:

                datos_productos.append([
                    Paragraph(
                        escape(str(producto["producto"])),
                        estilo_tabla
                    ),
                    str(producto["unidades_vendidas"]),
                    f"${producto['ingresos']:,.2f}"
                ])

            tabla_productos = LongTable(
                datos_productos,
                colWidths=[390, 150, 180],
                repeatRows=1
            )

            tabla_productos.setStyle(
                TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#075985")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("ALIGN", (1, 1), (-1, -1), "CENTER"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [
                        colors.white,
                        colors.HexColor("#f1f5f9")
                    ]),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7)
                ])
            )

            contenido.append(tabla_productos)

        else:

            contenido.append(
                Paragraph(
                    "No se encontraron productos vendidos en este periodo.",
                    estilo_normal
                )
            )

        # ==========================================
        # INGRESOS Y GANANCIAS MENSUALES
        # ==========================================

        contenido.append(
            Paragraph(
                "INGRESOS Y GANANCIAS POR MES",
                estilo_seccion
            )
        )

        if ventas_mensuales:

            datos_mensuales = [[
                "Año",
                "Mes",
                "Ingresos",
                "Ganancias",
                "Ventas sin costo"
            ]]

            for venta in ventas_mensuales:

                ganancia = (
                    f"${venta['ganancias']:,.2f}"
                    if venta["ganancias"] is not None
                    else "No disponible"
                )

                datos_mensuales.append([
                    str(venta["anio"]),
                    nombres_meses[venta["numero_mes"]],
                    f"${venta['ingresos']:,.2f}",
                    ganancia,
                    str(venta["ventas_sin_costo"])
                ])

            tabla_mensual = LongTable(
                datos_mensuales,
                colWidths=[90, 150, 160, 190, 130],
                repeatRows=1
            )

            tabla_mensual.setStyle(
                TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#075985")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [
                        colors.white,
                        colors.HexColor("#f1f5f9")
                    ]),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7)
                ])
            )

            contenido.append(tabla_mensual)

        else:

            contenido.append(
                Paragraph(
                    "No existen ventas mensuales para mostrar.",
                    estilo_normal
                )
            )

        # ==========================================
        # DETALLE DE VENTAS
        # ==========================================

        contenido.append(
            Paragraph("DETALLE DE VENTAS Y COMPRADORES", estilo_seccion)
        )

        if detalle_ventas:

            datos_detalle = [[
                "Pedido",
                "Fecha",
                "Comprador",
                "Producto",
                "Cantidad",
                "Precio unitario",
                "Ingreso",
                "Ganancia"
            ]]

            for venta in detalle_ventas:

                if venta["fecha_pedido"]:
                    fecha_venta = venta["fecha_pedido"].strftime(
                        "%d/%m/%Y %H:%M"
                    )
                else:
                    fecha_venta = "Sin fecha"

                ganancia = (
                    f"${venta['ganancia']:,.2f}"
                    if venta["ganancia"] is not None
                    else "No disponible"
                )

                datos_detalle.append([
                    f"#{venta['id_pedido']}",
                    fecha_venta,
                    Paragraph(
                        escape(str(venta["comprador"] or "No disponible")),
                        estilo_tabla
                    ),
                    Paragraph(
                        escape(str(venta["producto"])),
                        estilo_tabla
                    ),
                    str(venta["cantidad"]),
                    f"${venta['precio_unitario']:,.2f}",
                    f"${venta['subtotal']:,.2f}",
                    Paragraph(
                        escape(ganancia),
                        estilo_tabla
                    )
                ])

            tabla_detalle = LongTable(
                datos_detalle,
                colWidths=[
                    45, 75, 105, 145,
                    45, 80, 75, 105
                ],
                repeatRows=1
            )

            tabla_detalle.setStyle(
                TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#075985")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 7),
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [
                        colors.white,
                        colors.HexColor("#f1f5f9")
                    ]),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6)
                ])
            )

            contenido.append(tabla_detalle)

        else:

            contenido.append(
                Paragraph(
                    "No se encontraron ventas para mostrar.",
                    estilo_normal
                )
            )

        # ==========================================
        # PIE DE PÁGINA
        # ==========================================

        def agregar_pie_pagina(canvas, documento_pdf):

            canvas.saveState()

            ancho, alto = landscape(A4)

            canvas.setStrokeColor(
                colors.HexColor("#38bdf8")
            )

            canvas.setLineWidth(0.7)

            canvas.line(
                12 * mm,
                11 * mm,
                ancho - 12 * mm,
                11 * mm
            )

            canvas.setFont("Helvetica", 8)

            canvas.setFillColor(
                colors.HexColor("#64748b")
            )

            canvas.drawString(
                12 * mm,
                7 * mm,
                "MUNDO INFORMÁTICO | Reporte administrativo"
            )

            canvas.drawRightString(
                ancho - 12 * mm,
                7 * mm,
                f"Página {documento_pdf.page}"
            )

            canvas.restoreState()

        # ==========================================
        # GENERAR PDF
        # ==========================================

        documento.build(
            contenido,
            onFirstPage=agregar_pie_pagina,
            onLaterPages=agregar_pie_pagina
        )

        buffer.seek(0)

        return send_file(
            buffer,
            as_attachment=True,
            download_name="reporte_mundo_informatico.pdf",
            mimetype="application/pdf"
        )

    except Exception as error:

        print("ERROR AL GENERAR REPORTE PDF:", error)

        return "No se pudo generar el reporte PDF.", 500

    finally:

        if cursor is not None:
            cursor.close()

        if conexion is not None and conexion.is_connected():
            conexion.close()

# ==========================================
# INICIO Y CATÁLOGO
# ==========================================

@app.route("/")
def inicio():

    conexion = None
    cursor = None

    try:
        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        busqueda = request.args.get("q", "").strip()

        consulta = """
            SELECT
                p.id_producto,
                p.nombre,
                p.descripcion,
                p.precio,
                p.existencia,
                p.imagen,
                c.nombre AS categoria
            FROM productos AS p
            LEFT JOIN categorias AS c
                ON p.id_categoria = c.id_categoria
            WHERE p.activo = TRUE
        """

        parametros = ()

        if busqueda:
            consulta += """
                AND (
                    p.nombre LIKE %s
                    OR p.descripcion LIKE %s
                    OR c.nombre LIKE %s
                )
            """

            termino = f"%{busqueda}%"
            parametros = (termino, termino, termino)

        consulta += " ORDER BY p.id_producto ASC LIMIT 20"

        cursor.execute(consulta, parametros)

        productos = cursor.fetchall()

        cantidad_carrito = sum(
            session.get("carrito", {}).values()
        )

        return render_template(
            "index.html",
            productos=productos,
            busqueda=busqueda,
            cantidad_carrito=cantidad_carrito
        )

    except Exception as error:

        print("ERROR AL CARGAR PRODUCTOS:", error)
        return f"Error al cargar los productos: {error}", 500

    finally:

        if cursor is not None:
            cursor.close()

        if conexion is not None and conexion.is_connected():
            conexion.close()


# ==========================================
# AGREGAR PRODUCTOS AL CARRITO
# ==========================================

@app.route("/agregar-carrito/<int:id_producto>", methods=["POST"])
def agregar_carrito(id_producto):

    conexion = None
    cursor = None

    try:
        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        cursor.execute("""
            SELECT id_producto, nombre, existencia, activo
            FROM productos
            WHERE id_producto = %s
        """, (id_producto,))

        producto = cursor.fetchone()

        if not producto or not producto["activo"]:
            flash("El producto no está disponible.")
            return redirect(url_for("inicio"))

        carrito = session.get("carrito", {})
        id_texto = str(id_producto)

        cantidad_actual = carrito.get(id_texto, 0)

        if cantidad_actual >= producto["existencia"]:
            flash("No hay más existencias disponibles.")

        else:
            carrito[id_texto] = cantidad_actual + 1
            session["carrito"] = carrito
            flash("Producto agregado al carrito.")

        return redirect(url_for("inicio"))

    except Exception as error:

        print("ERROR AL AGREGAR AL CARRITO:", error)
        flash("No se pudo agregar el producto.")
        return redirect(url_for("inicio"))

    finally:

        if cursor is not None:
            cursor.close()

        if conexion is not None and conexion.is_connected():
            conexion.close()


# ==========================================
# MOSTRAR CARRITO
# ==========================================

@app.route("/carrito")
def ver_carrito():

    conexion = None
    cursor = None

    try:
        carrito = session.get("carrito", {})

        if not carrito:
            return render_template(
                "carrito.html",
                productos=[],
                total=0,
                cantidad_carrito=0
            )

        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        ids = list(carrito.keys())
        marcadores = ", ".join(["%s"] * len(ids))

        consulta = f"""
            SELECT id_producto, nombre, precio, existencia, imagen
            FROM productos
            WHERE id_producto IN ({marcadores})
            AND activo = TRUE
        """

        cursor.execute(
            consulta,
            tuple(int(i) for i in ids)
        )

        productos_db = cursor.fetchall()

        productos = []
        total = 0
        carrito_actualizado = {}

        for producto in productos_db:

            id_texto = str(producto["id_producto"])

            cantidad = min(
                carrito.get(id_texto, 0),
                producto["existencia"]
            )

            if cantidad <= 0:
                continue

            producto["cantidad"] = cantidad

            producto["subtotal"] = (
                float(producto["precio"]) * cantidad
            )

            total += producto["subtotal"]

            productos.append(producto)
            carrito_actualizado[id_texto] = cantidad

        session["carrito"] = carrito_actualizado

        cantidad_carrito = sum(carrito_actualizado.values())

        return render_template(
            "carrito.html",
            productos=productos,
            total=total,
            cantidad_carrito=cantidad_carrito
        )

    except Exception as error:

        print("ERROR AL MOSTRAR CARRITO:", error)
        return f"Error al mostrar el carrito: {error}", 500

    finally:

        if cursor is not None:
            cursor.close()

        if conexion is not None and conexion.is_connected():
            conexion.close()


# ==========================================
# ACTUALIZAR CANTIDAD
# ==========================================

@app.route(
    "/actualizar-carrito/<int:id_producto>",
    methods=["POST"]
)
def actualizar_carrito(id_producto):

    conexion = None
    cursor = None

    try:
        cantidad = int(request.form.get("cantidad", 1))

        if cantidad < 1:
            flash("La cantidad debe ser mayor a cero.")
            return redirect(url_for("ver_carrito"))

        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        cursor.execute("""
            SELECT existencia
            FROM productos
            WHERE id_producto = %s
            AND activo = TRUE
        """, (id_producto,))

        producto = cursor.fetchone()

        carrito = session.get("carrito", {})
        id_texto = str(id_producto)

        if not producto:
            carrito.pop(id_texto, None)
            flash("El producto ya no está disponible.")

        else:
            cantidad = min(cantidad, producto["existencia"])

            if cantidad == 0:
                carrito.pop(id_texto, None)
                flash("El producto no tiene existencias.")

            else:
                carrito[id_texto] = cantidad
                flash("Cantidad actualizada.")

        session["carrito"] = carrito

        return redirect(url_for("ver_carrito"))

    except ValueError:

        flash("Introduce una cantidad válida.")
        return redirect(url_for("ver_carrito"))

    except Exception as error:

        print("ERROR AL ACTUALIZAR CARRITO:", error)
        flash("No se pudo actualizar la cantidad.")
        return redirect(url_for("ver_carrito"))

    finally:

        if cursor is not None:
            cursor.close()

        if conexion is not None and conexion.is_connected():
            conexion.close()


# ==========================================
# ELIMINAR PRODUCTO DEL CARRITO
# ==========================================

@app.route(
    "/eliminar-carrito/<int:id_producto>",
    methods=["POST"]
)
def eliminar_carrito(id_producto):

    carrito = session.get("carrito", {})
    carrito.pop(str(id_producto), None)

    session["carrito"] = carrito

    flash("Producto eliminado del carrito.")

    return redirect(url_for("ver_carrito"))


# ==========================================
# VACIAR CARRITO
# ==========================================

@app.route("/vaciar-carrito", methods=["POST"])
def vaciar_carrito():

    session["carrito"] = {}

    flash("El carrito está vacío.")

    return redirect(url_for("ver_carrito"))


# ==========================================
# REGISTRO DE CLIENTES
# ==========================================

@app.route("/registro", methods=["GET", "POST"])
def registro():

    if session.get("usuario_id"):
        return redirect(url_for("inicio"))

    if request.method == "POST":

        nombre = request.form.get("nombre", "").strip()
        correo = request.form.get("correo", "").strip().lower()
        contrasena = request.form.get("contrasena", "")
        confirmar = request.form.get("confirmar", "")

        if not nombre or not correo or not contrasena or not confirmar:
            flash("Todos los campos son obligatorios.")
            return redirect(url_for("registro"))

        if len(nombre) > 100 or len(correo) > 150:
            flash("El nombre o correo es demasiado largo.")
            return redirect(url_for("registro"))

        if len(contrasena) < 8:
            flash("La contraseña debe tener al menos 8 caracteres.")
            return redirect(url_for("registro"))

        if contrasena != confirmar:
            flash("Las contraseñas no coinciden.")
            return redirect(url_for("registro"))

        conexion = None
        cursor = None

        try:
            conexion = obtener_conexion()
            cursor = conexion.cursor(dictionary=True)

            cursor.execute("""
                SELECT id_usuario
                FROM usuarios
                WHERE correo = %s
            """, (correo,))

            if cursor.fetchone():
                flash("Este correo ya está registrado.")
                return redirect(url_for("registro"))

            contrasena_segura = generate_password_hash(contrasena)

            cursor.execute("""
                INSERT INTO usuarios
                (nombre, correo, contrasena, perfil)
                VALUES (%s, %s, %s, 'cliente')
            """, (nombre, correo, contrasena_segura))

            conexion.commit()

            flash(
                "Cuenta creada correctamente. "
                "Ahora puedes iniciar sesión."
            )

            return redirect(url_for("login"))

        except Exception as error:

            if conexion is not None:
                conexion.rollback()

            print("ERROR EN REGISTRO:", error)
            flash("No se pudo crear la cuenta. Inténtalo nuevamente.")

        finally:

            if cursor is not None:
                cursor.close()

            if conexion is not None and conexion.is_connected():
                conexion.close()

    return render_template("registro.html")


# ==========================================
# INICIO DE SESIÓN
# ==========================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if session.get("usuario_id"):

        if session.get("usuario_perfil") == "administrador":
            return redirect(url_for("admin"))

        if session.get("carrito"):
            return redirect(url_for("finalizar_compra"))

        return redirect(url_for("inicio"))

    if request.method == "POST":

        correo = request.form.get("correo", "").strip().lower()
        contrasena = request.form.get("contrasena", "")

        if not correo or not contrasena:
            flash("Introduce tu correo y contraseña.")
            return redirect(url_for("login"))

        conexion = None
        cursor = None

        try:
            conexion = obtener_conexion()
            cursor = conexion.cursor(dictionary=True)

            cursor.execute("""
                SELECT
                    id_usuario,
                    nombre,
                    correo,
                    contrasena,
                    perfil
                FROM usuarios
                WHERE correo = %s
            """, (correo,))

            usuario = cursor.fetchone()

            if usuario and check_password_hash(
                usuario["contrasena"],
                contrasena
            ):

                carrito = session.get("carrito", {})

                session.clear()

                session["carrito"] = carrito
                session["usuario_id"] = usuario["id_usuario"]
                session["usuario_nombre"] = usuario["nombre"]
                session["usuario_perfil"] = usuario["perfil"]

                flash("Bienvenido, " + usuario["nombre"] + ".")

                if usuario["perfil"] == "administrador":
                    return redirect(url_for("admin"))

                if carrito:
                    return redirect(url_for("finalizar_compra"))

                return redirect(url_for("inicio"))

            flash("Correo o contraseña incorrectos.")

        except Exception as error:

            print("ERROR EN LOGIN:", error)
            flash("No se pudo iniciar sesión.")

        finally:

            if cursor is not None:
                cursor.close()

            if conexion is not None and conexion.is_connected():
                conexion.close()

    return render_template("login.html")


# ==========================================
# CERRAR SESIÓN
# ==========================================

@app.route("/logout")
def logout():

    carrito = session.get("carrito", {})

    session.clear()
    session["carrito"] = carrito

    flash("Has cerrado sesión correctamente.")

    return redirect(url_for("inicio"))


# ==========================================
# FINALIZAR COMPRA
# ==========================================

@app.route("/finalizar-compra")
def finalizar_compra():

    if not session.get("usuario_id"):
        flash("Inicia sesión para continuar con tu compra.")
        return redirect(url_for("login"))

    carrito = session.get("carrito", {})

    if not carrito:
        flash("Tu carrito está vacío.")
        return redirect(url_for("ver_carrito"))

    conexion = None
    cursor = None

    try:
        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        ids = [int(i) for i in carrito.keys()]
        marcadores = ", ".join(["%s"] * len(ids))

        cursor.execute(f"""
            SELECT
                id_producto,
                nombre,
                precio,
                existencia,
                imagen
            FROM productos
            WHERE id_producto IN ({marcadores})
            AND activo = TRUE
        """, tuple(ids))

        productos_db = cursor.fetchall()

        if len(productos_db) != len(carrito):
            flash("Uno o más productos ya no están disponibles.")
            return redirect(url_for("ver_carrito"))

        productos = []
        total = Decimal("0.00")

        for producto in productos_db:

            id_texto = str(producto["id_producto"])
            cantidad = int(carrito.get(id_texto, 0))

            if cantidad <= 0:
                continue

            if cantidad > producto["existencia"]:
                flash(
                    "La cantidad disponible de "
                    + producto["nombre"]
                    + " ha cambiado. Revisa tu carrito."
                )
                return redirect(url_for("ver_carrito"))

            producto["cantidad"] = cantidad

            producto["subtotal"] = (
                Decimal(str(producto["precio"])) * cantidad
            )

            total += producto["subtotal"]
            productos.append(producto)

        return render_template(
            "compra.html",
            productos=productos,
            total=total
        )

    except Exception as error:

        print("ERROR AL PREPARAR COMPRA:", error)
        flash("No se pudo cargar el formulario de compra.")
        return redirect(url_for("ver_carrito"))

    finally:

        if cursor is not None:
            cursor.close()

        if conexion is not None and conexion.is_connected():
            conexion.close()


# ==========================================
# CONFIRMAR Y REGISTRAR PEDIDO
# ==========================================



@app.route("/confirmar-pedido", methods=["POST"])
def confirmar_pedido():

    if not session.get("usuario_id"):
        flash("Inicia sesión para continuar.")
        return redirect(url_for("login"))

    carrito = session.get("carrito", {})

    if not carrito:
        flash("Tu carrito está vacío.")
        return redirect(url_for("ver_carrito"))

    nombre = request.form.get("nombre_receptor", "").strip()
    lada = request.form.get("lada_pais", "").strip()
    telefono = request.form.get("telefono_contacto", "").strip()
    direccion = request.form.get("direccion_entrega", "").strip()
    referencias = request.form.get("referencias", "").strip()

    # Ladas permitidas en el formulario.
    ladas_permitidas = {
        "+52", "+1", "+502", "+34", "+57", "+54"
    }

    if not nombre or not telefono or not direccion:
        flash("Completa todos los datos obligatorios.")
        return redirect(url_for("finalizar_compra"))

    # Validar lada internacional.
    if lada not in ladas_permitidas:
        flash("Selecciona una lada internacional válida.")
        return redirect(url_for("finalizar_compra"))

    # Validar que el teléfono tenga únicamente dígitos.
    if (
        not telefono.isascii()
        or not telefono.isdecimal()
        or not 1 <= len(telefono) <= 10
    ):
        flash("El teléfono debe contener entre 1 y 10 dígitos.")
        return redirect(url_for("finalizar_compra"))

    # Combinar lada internacional y número telefónico.
    telefono_completo = f"{lada} {telefono}"

    if (
        len(nombre) > 150
        or len(telefono_completo) > 20
        or len(direccion) > 500
        or len(referencias) > 500
    ):
        flash("Uno de los datos supera la longitud permitida.")
        return redirect(url_for("finalizar_compra"))

    conexion = None
    cursor = None

    try:
        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        conexion.start_transaction()

        ids = [int(i) for i in carrito.keys()]
        marcadores = ", ".join(["%s"] * len(ids))

        cursor.execute(f"""
            SELECT
                id_producto,
                nombre,
                precio,
                costo_compra,
                existencia,
                activo
            FROM productos
            WHERE id_producto IN ({marcadores})
            FOR UPDATE
        """, tuple(ids))

        productos_db = cursor.fetchall()

        if len(productos_db) != len(carrito):
            raise ValueError("Uno o más productos ya no existen.")

        productos_compra = []
        total = Decimal("0.00")

        for producto in productos_db:

            id_texto = str(producto["id_producto"])
            cantidad = int(carrito.get(id_texto, 0))

            if not producto["activo"]:
                raise ValueError(
                    producto["nombre"] + " ya no está disponible."
                )

            if cantidad <= 0 or cantidad > producto["existencia"]:
                raise ValueError(
                    "Existencias insuficientes para "
                    + producto["nombre"] + "."
                )

            precio = Decimal(str(producto["precio"]))

            # Guardar el costo de adquisición del producto.
            # Si todavía no tiene costo registrado, conservar NULL.
            costo_compra = (
                Decimal(str(producto["costo_compra"]))
                if producto["costo_compra"] is not None
                else None
            )

            total += precio * cantidad

            productos_compra.append({
                "id_producto": producto["id_producto"],
                "cantidad": cantidad,
                "precio": precio,
                "costo_compra": costo_compra,
                "existencia_anterior": producto["existencia"],
                "existencia_nueva": producto["existencia"] - cantidad
            })

        cursor.execute("""
            INSERT INTO pedidos (
                id_usuario,
                total,
                estado,
                nombre_receptor,
                telefono_contacto,
                direccion_entrega,
                referencias,
                metodo_pago
            )
            VALUES (
                %s, %s, 'pendiente', %s, %s, %s, %s,
                'contra_entrega'
            )
        """, (
            session["usuario_id"],
            total,
            nombre,
            telefono_completo,
            direccion,
            referencias
        ))

        id_pedido = cursor.lastrowid

        for producto in productos_compra:

            # Guardar precio de venta y costo histórico.
            cursor.execute("""
                INSERT INTO detalle_pedido (
                    id_pedido,
                    id_producto,
                    cantidad,
                    precio_unitario,
                    costo_unitario
                )
                VALUES (%s, %s, %s, %s, %s)
            """, (
                id_pedido,
                producto["id_producto"],
                producto["cantidad"],
                producto["precio"],
                producto["costo_compra"]
            ))

            cursor.execute("""
                UPDATE productos
                SET existencia = existencia - %s
                WHERE id_producto = %s
                AND existencia >= %s
            """, (
                producto["cantidad"],
                producto["id_producto"],
                producto["cantidad"]
            ))

            if cursor.rowcount != 1:
                raise ValueError(
                    "No se pudieron actualizar las existencias."
                )

            # Registrar la salida generada por la venta.
            cursor.execute("""
                INSERT INTO historial_inventario (
                    id_producto,
                    id_usuario,
                    tipo_movimiento,
                    cantidad,
                    existencia_anterior,
                    existencia_nueva,
                    motivo
                )
                VALUES (%s, %s, 'venta', %s, %s, %s, %s)
            """, (
                producto["id_producto"],
                session["usuario_id"],
                producto["cantidad"],
                producto["existencia_anterior"],
                producto["existencia_nueva"],
                f"Venta correspondiente al pedido #{id_pedido}"
            ))

        conexion.commit()

        session["ultimo_pedido"] = id_pedido
        session["carrito"] = {}

        flash("¡Tu pedido se registró correctamente!")

        return redirect(
            url_for("pedido_confirmado", id_pedido=id_pedido)
        )

    except ValueError as error:

        if conexion is not None:
            conexion.rollback()

        flash(str(error))
        return redirect(url_for("ver_carrito"))

    except Exception as error:

        if conexion is not None:
            conexion.rollback()

        print("ERROR AL CONFIRMAR PEDIDO:", error)

        flash(
            "Ocurrió un error al registrar tu pedido. "
            "Inténtalo nuevamente."
        )

        return redirect(url_for("ver_carrito"))

    finally:

        if cursor is not None:
            cursor.close()

        if conexion is not None and conexion.is_connected():
            conexion.close()

# ==========================================
# COMPROBANTE DEL PEDIDO
# ==========================================

@app.route("/pedido-confirmado/<int:id_pedido>")
def pedido_confirmado(id_pedido):

    if not session.get("usuario_id"):
        flash("Inicia sesión para consultar tu pedido.")
        return redirect(url_for("login"))

    conexion = None
    cursor = None

    try:
        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                id_pedido,
                id_usuario,
                fecha_pedido,
                total,
                estado,
                nombre_receptor,
                telefono_contacto,
                direccion_entrega,
                referencias,
                metodo_pago
            FROM pedidos
            WHERE id_pedido = %s
            AND id_usuario = %s
        """, (id_pedido, session["usuario_id"]))

        pedido = cursor.fetchone()

        if not pedido:
            flash("No se encontró el pedido solicitado.")
            return redirect(url_for("inicio"))

        cursor.execute("""
            SELECT
                p.nombre,
                dp.cantidad,
                dp.precio_unitario,
                (dp.cantidad * dp.precio_unitario) AS subtotal
            FROM detalle_pedido AS dp
            INNER JOIN productos AS p
                ON dp.id_producto = p.id_producto
            WHERE dp.id_pedido = %s
        """, (id_pedido,))

        productos = cursor.fetchall()

        if not productos:
            flash("El pedido no contiene productos.")
            return redirect(url_for("inicio"))

        return render_template(
            "pedido_confirmado.html",
            pedido=pedido,
            productos=productos
        )

    except Exception as error:

        print("ERROR AL MOSTRAR COMPROBANTE:", error)
        return "Error al generar el comprobante del pedido.", 500

    finally:

        if cursor is not None:
            cursor.close()

        if conexion is not None and conexion.is_connected():
            conexion.close()


# ==========================================
# PANEL PRINCIPAL DE ADMINISTRACIÓN
# ==========================================

@app.route("/admin")
@administrador_requerido
def admin():

    conexion = None
    cursor = None

    try:
        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        cursor.execute("""
            SELECT COUNT(*) AS total_pedidos
            FROM pedidos
        """)

        total_pedidos = cursor.fetchone()["total_pedidos"]

        cursor.execute("""
            SELECT COUNT(*) AS pendientes
            FROM pedidos
            WHERE estado = 'pendiente'
        """)

        pendientes = cursor.fetchone()["pendientes"]

        cursor.execute("""
            SELECT COUNT(*) AS total_clientes
            FROM usuarios
            WHERE perfil = 'cliente'
        """)

        total_clientes = cursor.fetchone()["total_clientes"]

        cursor.execute("""
            SELECT COALESCE(SUM(total), 0) AS ventas
            FROM pedidos
            WHERE estado IN ('pagado', 'enviado', 'entregado')
        """)

        ventas = cursor.fetchone()["ventas"]

        cursor.execute("""
            SELECT
                p.id_pedido,
                p.fecha_pedido,
                p.total,
                p.estado,
                u.nombre AS cliente
            FROM pedidos AS p
            INNER JOIN usuarios AS u
                ON p.id_usuario = u.id_usuario
            ORDER BY p.id_pedido DESC
            LIMIT 5
        """)

        pedidos_recientes = cursor.fetchall()

        return render_template(
            "admin.html",
            total_pedidos=total_pedidos,
            pendientes=pendientes,
            total_clientes=total_clientes,
            ventas=ventas,
            pedidos_recientes=pedidos_recientes
        )

    except Exception as error:

        print("ERROR EN PANEL ADMIN:", error)
        return "No se pudo cargar el panel administrativo.", 500

    finally:

        if cursor is not None:
            cursor.close()

        if conexion is not None and conexion.is_connected():
            conexion.close()


# ==========================================
# LISTA DE PEDIDOS
# ==========================================

@app.route("/admin/pedidos")
@administrador_requerido
def admin_pedidos():

    conexion = None
    cursor = None

    try:
        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                p.id_pedido,
                p.fecha_pedido,
                p.total,
                p.estado,
                p.nombre_receptor,
                u.nombre AS cliente,
                u.correo
            FROM pedidos AS p
            INNER JOIN usuarios AS u
                ON p.id_usuario = u.id_usuario
            ORDER BY p.id_pedido DESC
        """)

        pedidos = cursor.fetchall()

        return render_template(
            "admin_pedidos.html",
            pedidos=pedidos
        )

    except Exception as error:

        print("ERROR AL CONSULTAR PEDIDOS:", error)
        return "No se pudieron consultar los pedidos.", 500

    finally:

        if cursor is not None:
            cursor.close()

        if conexion is not None and conexion.is_connected():
            conexion.close()


# ==========================================
# DETALLES DE UN PEDIDO
# ==========================================

@app.route("/admin/pedido/<int:id_pedido>")
@administrador_requerido
def admin_detalle_pedido(id_pedido):

    conexion = None
    cursor = None

    try:
        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                p.*,
                u.nombre AS cliente,
                u.correo AS correo_cliente
            FROM pedidos AS p
            INNER JOIN usuarios AS u
                ON p.id_usuario = u.id_usuario
            WHERE p.id_pedido = %s
        """, (id_pedido,))

        pedido = cursor.fetchone()

        if not pedido:
            flash("El pedido solicitado no existe.")
            return redirect(url_for("admin_pedidos"))

        cursor.execute("""
            SELECT
                p.nombre,
                dp.cantidad,
                dp.precio_unitario,
                (dp.cantidad * dp.precio_unitario) AS subtotal
            FROM detalle_pedido AS dp
            INNER JOIN productos AS p
                ON dp.id_producto = p.id_producto
            WHERE dp.id_pedido = %s
        """, (id_pedido,))

        productos = cursor.fetchall()

        return render_template(
            "admin_detalle_pedido.html",
            pedido=pedido,
            productos=productos
        )

    except Exception as error:

        print("ERROR AL CONSULTAR DETALLE:", error)
        return "No se pudo consultar el pedido.", 500

    finally:

        if cursor is not None:
            cursor.close()

        if conexion is not None and conexion.is_connected():
            conexion.close()




# ==========================================
# ACTUALIZAR ESTADO DEL PEDIDO
# ==========================================

@app.route(
    "/admin/pedido/<int:id_pedido>/estado",
    methods=["POST"]
)
@administrador_requerido
def admin_actualizar_estado(id_pedido):

    conexion = None
    cursor = None

    try:
        nuevo_estado = request.form.get("estado", "").strip()

        # Solamente se permite confirmar o cancelar.
        if nuevo_estado not in ("confirmado", "cancelado"):
            flash("Acción no válida.")
            return redirect(
                url_for("admin_detalle_pedido", id_pedido=id_pedido)
            )

        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        conexion.start_transaction()

        # Bloquear el pedido para evitar modificaciones simultáneas.
        cursor.execute("""
            SELECT id_pedido, estado
            FROM pedidos
            WHERE id_pedido = %s
            FOR UPDATE
        """, (id_pedido,))

        pedido = cursor.fetchone()

        if not pedido:
            raise ValueError("El pedido no existe.")

        # Solo se permiten acciones sobre pedidos pendientes.
        if pedido["estado"] != "pendiente":
            raise ValueError(
                "Este pedido ya fue procesado y no se puede modificar."
            )

        # ==========================================
        # CONFIRMAR PEDIDO
        # ==========================================

        if nuevo_estado == "confirmado":

            cursor.execute("""
                UPDATE pedidos
                SET estado = 'confirmado'
                WHERE id_pedido = %s
                AND estado = 'pendiente'
            """, (id_pedido,))

            if cursor.rowcount != 1:
                raise ValueError("No se pudo confirmar el pedido.")

            conexion.commit()

            flash(
                "Pedido confirmado correctamente. "
                "Ahora está protegido contra modificaciones."
            )

        # ==========================================
        # CANCELAR PEDIDO
        # ==========================================

        elif nuevo_estado == "cancelado":

            # Recuperar los productos del pedido.
            cursor.execute("""
                SELECT
                    dp.id_producto,
                    dp.cantidad,
                    p.nombre,
                    p.existencia
                FROM detalle_pedido AS dp
                INNER JOIN productos AS p
                    ON dp.id_producto = p.id_producto
                WHERE dp.id_pedido = %s
                FOR UPDATE
            """, (id_pedido,))

            productos = cursor.fetchall()

            if not productos:
                raise ValueError("El pedido no contiene productos.")

            # Devolver cada producto al inventario.
            for producto in productos:

                existencia_anterior = producto["existencia"]
                existencia_nueva = (
                    existencia_anterior + producto["cantidad"]
                )

                cursor.execute("""
                    UPDATE productos
                    SET existencia = %s
                    WHERE id_producto = %s
                """, (
                    existencia_nueva,
                    producto["id_producto"]
                ))

                # Registrar la devolución en el historial.
                cursor.execute("""
                    INSERT INTO historial_inventario (
                        id_producto,
                        id_usuario,
                        tipo_movimiento,
                        cantidad,
                        existencia_anterior,
                        existencia_nueva,
                        motivo
                    )
                    VALUES (%s, %s, 'entrada', %s, %s, %s, %s)
                """, (
                    producto["id_producto"],
                    session["usuario_id"],
                    producto["cantidad"],
                    existencia_anterior,
                    existencia_nueva,
                    f"Devolución por cancelación del pedido #{id_pedido}"
                ))

            # Cambiar el estado del pedido.
            cursor.execute("""
                UPDATE pedidos
                SET estado = 'cancelado'
                WHERE id_pedido = %s
                AND estado = 'pendiente'
            """, (id_pedido,))

            if cursor.rowcount != 1:
                raise ValueError("No se pudo cancelar el pedido.")

            conexion.commit()

            flash(
                "Pedido cancelado correctamente. "
                "Las existencias fueron devueltas al inventario."
            )

    except ValueError as error:

        if conexion is not None:
            conexion.rollback()

        flash(str(error))

    except Exception as error:

        if conexion is not None:
            conexion.rollback()

        print("ERROR AL ACTUALIZAR PEDIDO:", error)
        flash("No se pudo actualizar el pedido.")

    finally:

        if cursor is not None:
            cursor.close()

        if conexion is not None and conexion.is_connected():
            conexion.close()

    return redirect(
        url_for("admin_detalle_pedido", id_pedido=id_pedido)
    )





@app.route("/chatbot", methods=["POST"])
def chatbot():

    conexion = None
    cursor = None

    try:
        import re
        import unicodedata

        # ==========================================
        # NORMALIZAR TEXTO
        # ==========================================

        def normalizar(texto):
            texto = str(texto).lower()
            texto = unicodedata.normalize("NFD", texto)

            return "".join(
                caracter for caracter in texto
                if unicodedata.category(caracter) != "Mn"
            )

        # ==========================================
        # RECIBIR MENSAJE
        # ==========================================

        datos = request.get_json(silent=True) or {}
        mensaje = str(datos.get("mensaje", "")).strip()

        if not mensaje:
            return jsonify({
                "respuesta": "Escribe una pregunta para poder ayudarte."
            }), 400

        if len(mensaje) > 500:
            return jsonify({
                "respuesta": "Tu pregunta es demasiado larga."
            }), 400

        mensaje_normalizado = normalizar(mensaje)

        # ==========================================
        # SALUDOS
        # ==========================================

        if any(palabra in mensaje_normalizado for palabra in [
            "hola", "buenos dias", "buenas tardes",
            "buenas noches", "saludos"
        ]):

            return jsonify({
                "respuesta": (
                    "¡Hola! Bienvenido a MUNDO INFORMÁTICO. "
                    "Puedo ayudarte a consultar productos, precios, "
                    "existencias, especificaciones técnicas y "
                    "recomendaciones de compra. "
                    "¿Qué estás buscando?"
                )
            })

        # ==========================================
        # LIMPIAR PALABRAS
        # ==========================================

        palabras_ignoradas = {
            "que", "como", "cuanto", "cuesta", "precio",
            "precios", "tienen", "tiene", "hay", "quiero",
            "buscar", "busco", "necesito", "una", "uno",
            "unos", "unas", "los", "las", "del", "con",
            "por", "para", "de", "el", "la", "un", "en",
            "me", "pueden", "puedo", "venden", "disponible",
            "disponibles", "producto", "productos", "favor",
            "informacion", "mostrar", "muestrame", "ver",
            "dime", "tienes", "mas", "mejor", "cual",
            "cuales", "entre", "diferencia", "diferencias",
            "comparar", "compara", "comparacion", "es",
            "son", "ser", "tiene", "tienen"
        }

        palabras = re.findall(
            r"[a-z0-9ñ]{2,}",
            mensaje_normalizado
        )

        palabras = [
            palabra for palabra in palabras
            if palabra not in palabras_ignoradas
        ]

        # ==========================================
        # CONVERTIR PLURALES
        # ==========================================

        equivalencias = {
            "laptops": "laptop",
            "computadoras": "computadora",
            "monitores": "monitor",
            "teclados": "teclado",
            "bocinas": "bocina",
            "audifonos": "audifono",
            "memorias": "memoria",
            "discos": "disco",
            "cables": "cable",
            "mouses": "mouse",
            "tarjetas": "tarjeta"
        }

        palabras = [
            equivalencias.get(palabra, palabra)
            for palabra in palabras
        ]

        palabras = list(dict.fromkeys(palabras))

        # ==========================================
        # CONECTAR CON MYSQL
        # ==========================================

        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        consulta_base = """
            SELECT
                p.id_producto,
                p.nombre,
                p.descripcion,
                p.precio,
                p.existencia,
                p.marca,
                p.procesador,
                p.memoria_ram,
                p.almacenamiento,
                p.tarjeta_grafica,
                p.pantalla,
                p.garantia,
                c.nombre AS categoria
            FROM productos p
            LEFT JOIN categorias c
                ON p.id_categoria = c.id_categoria
            WHERE p.activo = TRUE
        """

        # ==========================================
        # OBTENER ESPECIFICACIONES
        # ==========================================

        def obtener_especificaciones(producto):

            especificaciones = [
                ("Marca", "marca"),
                ("Procesador", "procesador"),
                ("Memoria RAM", "memoria_ram"),
                ("Almacenamiento", "almacenamiento"),
                ("Tarjeta gráfica", "tarjeta_grafica"),
                ("Pantalla", "pantalla"),
                ("Garantía", "garantia")
            ]

            datos_tecnicos = []

            for etiqueta, campo in especificaciones:

                valor = producto.get(campo)

                if valor and str(valor).strip():

                    datos_tecnicos.append(
                        f"{etiqueta}: {valor}"
                    )

            return datos_tecnicos

        # ==========================================
        # FUNCIONES PARA ANALIZAR PRODUCTOS
        # ==========================================

        def obtener_capacidad(texto):

            coincidencia = re.search(
                r"(\d+(?:\.\d+)?)\s*(gb|tb)",
                str(texto).lower()
            )

            if not coincidencia:
                return None

            valor = float(coincidencia.group(1))

            if coincidencia.group(2) == "tb":
                valor *= 1024

            return valor

        def obtener_ram(producto):

            return obtener_capacidad(
                producto.get("memoria_ram") or ""
            ) or 0

        def obtener_almacenamiento(producto):

            return obtener_capacidad(
                producto.get("almacenamiento") or ""
            ) or 0

        def tiene_grafica_dedicada(producto):

            gpu = normalizar(
                producto.get("tarjeta_grafica") or ""
            )

            return any(nombre in gpu for nombre in [
                "rtx", "gtx", "radeon rx"
            ])

        # ==========================================
        # DETECTAR CARACTERÍSTICA TÉCNICA
        # ==========================================

        criterio = None

        if any(palabra in mensaje_normalizado for palabra in [
            "procesador", "cpu"
        ]):

            criterio = "procesador"

        elif any(palabra in mensaje_normalizado for palabra in [
            "memoria ram", "ram"
        ]):

            criterio = "ram"

        elif any(palabra in mensaje_normalizado for palabra in [
            "tarjeta grafica", "grafica", "gpu"
        ]):

            criterio = "grafica"

        elif "almacenamiento" in mensaje_normalizado:

            criterio = "almacenamiento"

        elif "pantalla" in mensaje_normalizado:

            criterio = "pantalla"

        # ==========================================
        # DETECTAR INTENCIONES DE RECOMENDACIÓN
        # ==========================================

        intenciones_recomendacion = {

            "juegos": [
                "para jugar",
                "para gaming",
                "para videojuegos",
                "jugar videojuegos",
                "juegos exigentes"
            ],

            "estudio": [
                "para estudiar",
                "para la escuela",
                "para tareas",
                "para trabajos escolares",
                "para uso escolar"
            ],

            "precio": [
                "por que cuesta mas",
                "por que es mas caro",
                "por que vale mas",
                "por que tiene ese precio",
                "por que cuesta tanto"
            ],

            "rendimiento": [
                "rendimiento general",
                "mejor rendimiento",
                "mas potente",
                "mas rendimiento"
            ],

            "conveniencia": [
                "vale la pena",
                "vale pagar",
                "conviene pagar",
                "pagar adicionales",
                "pagar de mas"
            ]
        }

        intencion = None

        for tipo, frases in intenciones_recomendacion.items():

            if any(
                frase in mensaje_normalizado
                for frase in frases
            ):

                intencion = tipo
                break

        # ==========================================
        # DETECTAR COMPARACIONES
        # ==========================================

        frases_comparacion = [
            "diferencia entre",
            "diferencias entre",
            "que diferencia hay entre",
            "cual es la diferencia entre",
            "comparar",
            "compara",
            "comparacion",
            "cual es mejor",
            "cual conviene",
            "cual me conviene",
            "mejor procesador",
            "mejor cpu",
            "que procesador es mejor",
            "mas ram",
            "mas memoria ram",
            "mejor tarjeta grafica",
            "mejor grafica",
            "mejor gpu",
            "mas almacenamiento",
            "mejor pantalla",
            "que laptop tiene mejor",
            "que computadora tiene mejor",
            "cual tiene mas",
            "cual tiene mejor"
        ]

        es_comparacion = any(
            frase in mensaje_normalizado
            for frase in frases_comparacion
        )

        # Las recomendaciones también necesitan
        # recuperar los productos comparados.

        if intencion:
            es_comparacion = True

        if criterio and any(palabra in mensaje_normalizado for palabra in [
            "mejor", "mas", "mayor", "cual",
            "comparar", "compara", "diferencia"
        ]):

            es_comparacion = True

        # ==========================================
        # COMPARAR PRODUCTOS
        # ==========================================

        if es_comparacion:

            cursor.execute(
                consulta_base + " ORDER BY p.nombre ASC"
            )

            catalogo = cursor.fetchall()

            productos_encontrados = []

            for producto in catalogo:

                nombre_normalizado = normalizar(
                    producto["nombre"]
                )

                # Evitar confundir características
                # con nombres de productos.

                if nombre_normalizado in [
                    "tarjeta grafica",
                    "memoria ram",
                    "procesador",
                    "almacenamiento",
                    "pantalla"
                ]:
                    continue

                patron = (
                    r"(?<!\w)"
                    + re.escape(nombre_normalizado)
                    + r"(?!\w)"
                )

                coincidencia = re.search(
                    patron,
                    mensaje_normalizado
                )

                if coincidencia:

                    productos_encontrados.append((
                        coincidencia.start(),
                        producto
                    ))

            productos_encontrados.sort(
                key=lambda elemento: elemento[0]
            )

            productos_encontrados = [
                producto
                for posicion, producto in productos_encontrados
            ]

            # Evitar productos repetidos.

            productos_unicos = []

            for producto in productos_encontrados:

                if not any(
                    existente["id_producto"] == producto["id_producto"]
                    for existente in productos_unicos
                ):

                    productos_unicos.append(producto)

            productos_encontrados = productos_unicos

            # ======================================
            # RECUPERAR ÚLTIMA COMPARACIÓN
            # ======================================

            if len(productos_encontrados) < 2:

                ultima_comparacion = session.get(
                    "ultima_comparacion"
                )

                if (
                    isinstance(ultima_comparacion, list)
                    and len(ultima_comparacion) == 2
                ):

                    cursor.execute(
                        consulta_base + """
                            AND p.id_producto IN (%s, %s)
                        """,
                        tuple(ultima_comparacion)
                    )

                    productos_guardados = cursor.fetchall()

                    # Mantener el orden original.

                    productos_guardados.sort(
                        key=lambda producto:
                        ultima_comparacion.index(
                            producto["id_producto"]
                        )
                    )

                    if len(productos_guardados) == 2:

                        productos_encontrados = productos_guardados

            # ======================================
            # VERIFICAR PRODUCTOS
            # ======================================

            if len(productos_encontrados) < 2:

                if len(productos_encontrados) == 1:

                    respuesta = (
                        "Encontré el producto "
                        f"{productos_encontrados[0]['nombre']}, "
                        "pero necesito que indiques el nombre "
                        "del segundo producto para compararlos."
                    )

                else:

                    respuesta = (
                        "Todavía no tengo una comparación anterior "
                        "para responder esa pregunta. Primero "
                        "indícame los dos productos que deseas comparar."
                    )

                return jsonify({"respuesta": respuesta})

            producto1 = productos_encontrados[0]
            producto2 = productos_encontrados[1]

            # Guardar la comparación actual.

            session["ultima_comparacion"] = [
                producto1["id_producto"],
                producto2["id_producto"]
            ]

            # ======================================
            # DATOS GENERALES
            # ======================================

            precio1 = float(producto1["precio"])
            precio2 = float(producto2["precio"])

            diferencia = abs(precio1 - precio2)

            if precio1 >= precio2:
                mas_caro = producto1
                mas_barato = producto2
            else:
                mas_caro = producto2
                mas_barato = producto1

            # ======================================
            # RECOMENDACIONES INTELIGENTES
            # ======================================

            if intencion:

                nombre1 = producto1["nombre"]
                nombre2 = producto2["nombre"]

                # ==================================
                # RECOMENDACIÓN PARA JUGAR
                # ==================================

                if intencion == "juegos":

                    gpu1 = tiene_grafica_dedicada(producto1)
                    gpu2 = tiene_grafica_dedicada(producto2)

                    if gpu1 and not gpu2:
                        recomendado = producto1

                    elif gpu2 and not gpu1:
                        recomendado = producto2

                    else:

                        ram1 = obtener_ram(producto1)
                        ram2 = obtener_ram(producto2)

                        if ram1 > ram2:
                            recomendado = producto1

                        elif ram2 > ram1:
                            recomendado = producto2

                        else:
                            recomendado = mas_caro

                    respuesta = (
                        "RECOMENDACIÓN PARA JUGAR\n\n"
                        f"Mi recomendación es {recomendado['nombre']}.\n\n"
                        "Estas son algunas de sus características:\n"
                        f"- Procesador: "
                        f"{recomendado.get('procesador') or 'No especificado'}.\n"
                        f"- Memoria RAM: "
                        f"{recomendado.get('memoria_ram') or 'No especificada'}.\n"
                        f"- Tarjeta gráfica: "
                        f"{recomendado.get('tarjeta_grafica') or 'No especificada'}.\n"
                    )

                    if tiene_grafica_dedicada(recomendado):

                        respuesta += (
                            "\nSu tarjeta gráfica dedicada "
                            "es una ventaja importante para "
                            "ejecutar videojuegos y aplicaciones "
                            "gráficas exigentes.\n"
                        )

                    else:

                        respuesta += (
                            "\nEste equipo utiliza gráficos integrados, "
                            "por lo que es más adecuado para juegos "
                            "ligeros y actividades cotidianas.\n"
                        )

                    if recomendado["existencia"] > 0:

                        respuesta += (
                            f"\nActualmente tenemos "
                            f"{recomendado['existencia']} "
                            "unidades disponibles."
                        )

                    else:

                        respuesta += (
                            "\nSin embargo, este producto "
                            "está agotado actualmente."
                        )

                        if mas_barato["existencia"] > 0:

                            respuesta += (
                                f"\nComo alternativa puedes "
                                f"considerar {mas_barato['nombre']}, "
                                "aunque su rendimiento puede ser menor."
                            )

                    respuesta += (
                        "\n\nSi tu prioridad son los videojuegos, "
                        "te recomiendo elegir el equipo con mejor "
                        "tarjeta gráfica y suficiente memoria RAM."
                    )

                # ==================================
                # RECOMENDACIÓN PARA ESTUDIAR
                # ==================================

                elif intencion == "estudio":

                    recomendado = mas_barato

                    respuesta = (
                        "RECOMENDACIÓN PARA ESTUDIAR\n\n"
                        f"Para actividades escolares, mi primera "
                        f"opción sería {recomendado['nombre']}.\n\n"
                        "Para realizar tareas, documentos, "
                        "presentaciones, investigaciones y navegar "
                        "por internet, no siempre es necesario "
                        "comprar el equipo más potente.\n\n"
                        "Sus características son:\n"
                        f"- Procesador: "
                        f"{recomendado.get('procesador') or 'No especificado'}.\n"
                        f"- RAM: "
                        f"{recomendado.get('memoria_ram') or 'No especificada'}.\n"
                        f"- Almacenamiento: "
                        f"{recomendado.get('almacenamiento') or 'No especificado'}.\n"
                        f"- Precio: "
                        f"${float(recomendado['precio']):,.2f} MXN.\n"
                    )

                    if recomendado["existencia"] <= 0:

                        respuesta += (
                            "\nActualmente este producto está agotado.\n"
                        )

                        if mas_caro["existencia"] > 0:

                            respuesta += (
                                f"Como alternativa disponible puedes "
                                f"considerar {mas_caro['nombre']}. "
                                "Es una opción más potente, aunque "
                                "también tiene un precio mayor.\n"
                            )

                    else:

                        respuesta += (
                            f"\nTenemos {recomendado['existencia']} "
                            "unidades disponibles.\n"
                        )

                    respuesta += (
                        "\nMi consejo: si solamente necesitas "
                        "realizar actividades escolares y de oficina, "
                        "no necesitas pagar de más por potencia "
                        "que posiblemente no vas a utilizar."
                    )

                # ==================================
                # EXPLICAR DIFERENCIA DE PRECIO
                # ==================================

                elif intencion == "precio":

                    respuesta = (
                        "¿POR QUÉ EXISTE UNA DIFERENCIA DE PRECIO?\n\n"
                        f"{mas_caro['nombre']} cuesta "
                        f"${diferencia:,.2f} MXN más que "
                        f"{mas_barato['nombre']}.\n\n"
                        "La diferencia puede explicarse por "
                        "sus componentes y características:\n\n"
                    )

                    campos = [
                        ("Procesador", "procesador"),
                        ("Memoria RAM", "memoria_ram"),
                        ("Almacenamiento", "almacenamiento"),
                        ("Tarjeta gráfica", "tarjeta_grafica"),
                        ("Pantalla", "pantalla")
                    ]

                    for etiqueta, campo in campos:

                        valor_caro = mas_caro.get(campo)
                        valor_barato = mas_barato.get(campo)

                        if valor_caro != valor_barato:

                            respuesta += (
                                f"- {etiqueta}:\n"
                                f"  {mas_caro['nombre']}: "
                                f"{valor_caro or 'Sin especificar'}.\n"
                                f"  {mas_barato['nombre']}: "
                                f"{valor_barato or 'Sin especificar'}.\n"
                            )

                    respuesta += (
                        "\nPor lo tanto, el precio superior puede "
                        "justificarse por las características de "
                        "hardware que ofrece el producto más caro. "
                        "Sin embargo, eso no significa que siempre "
                        "sea la mejor compra para todos los usuarios."
                    )

                # ==================================
                # RENDIMIENTO GENERAL
                # ==================================

                elif intencion == "rendimiento":

                    puntos1 = 0
                    puntos2 = 0

                    # Gráficos dedicados.

                    if tiene_grafica_dedicada(producto1):
                        puntos1 += 3

                    if tiene_grafica_dedicada(producto2):
                        puntos2 += 3

                    # Memoria RAM.

                    ram1 = obtener_ram(producto1)
                    ram2 = obtener_ram(producto2)

                    if ram1 > ram2:
                        puntos1 += 2

                    elif ram2 > ram1:
                        puntos2 += 2

                    # Procesadores registrados.

                    cpu1 = normalizar(
                        producto1.get("procesador") or ""
                    )

                    cpu2 = normalizar(
                        producto2.get("procesador") or ""
                    )

                    if "i5-12450h" in cpu1:
                        puntos1 += 2

                    if "i5-12450h" in cpu2:
                        puntos2 += 2

                    if "ryzen 5 5500u" in cpu1:
                        puntos2 += 0

                    if "ryzen 5 5500u" in cpu2:
                        puntos1 += 0

                    # Almacenamiento.

                    almacenamiento1 = obtener_almacenamiento(producto1)
                    almacenamiento2 = obtener_almacenamiento(producto2)

                    if almacenamiento1 > almacenamiento2:
                        puntos1 += 1

                    elif almacenamiento2 > almacenamiento1:
                        puntos2 += 1

                    respuesta = (
                        "ANÁLISIS DE RENDIMIENTO GENERAL\n\n"
                        f"{nombre1}: {puntos1} puntos de evaluación.\n"
                        f"{nombre2}: {puntos2} puntos de evaluación.\n\n"
                    )

                    if puntos1 > puntos2:

                        respuesta += (
                            f"Considerando las características "
                            f"registradas, {nombre1} ofrece una "
                            "configuración más potente en esta comparación."
                        )

                    elif puntos2 > puntos1:

                        respuesta += (
                            f"Considerando las características "
                            f"registradas, {nombre2} ofrece una "
                            "configuración más potente en esta comparación."
                        )

                    else:

                        respuesta += (
                            "Ambos productos tienen resultados similares "
                            "con los criterios registrados. No hay una "
                            "ventaja general suficientemente clara."
                        )

                    respuesta += (
                        "\n\nEsta evaluación es orientativa y no "
                        "representa una prueba de rendimiento real."
                    )

                # ==================================
                # ¿VALE LA PENA PAGAR MÁS?
                # ==================================

                elif intencion == "conveniencia":

                    respuesta = (
                        "¿VALE LA PENA PAGAR LA DIFERENCIA?\n\n"
                        f"La diferencia entre ambos productos es de "
                        f"${diferencia:,.2f} MXN.\n\n"
                    )

                    if (
                        tiene_grafica_dedicada(mas_caro)
                        and not tiene_grafica_dedicada(mas_barato)
                    ):

                        respuesta += (
                            f"Si quieres jugar videojuegos, utilizar "
                            f"programas gráficos o realizar tareas "
                            f"exigentes, pagar más por "
                            f"{mas_caro['nombre']} puede ser una "
                            "inversión conveniente.\n\n"
                            "La tarjeta gráfica dedicada representa "
                            "una diferencia importante entre ambos equipos."
                        )

                    else:

                        respuesta += (
                            f"{mas_caro['nombre']} cuesta más, pero "
                            "conviene revisar si sus características "
                            "adicionales realmente son necesarias "
                            "para el uso que le darás.\n\n"
                            "Si solamente necesitas actividades básicas, "
                            "el producto económico podría ser suficiente."
                        )

                    respuesta += (
                        "\n\nMi recomendación es no pagar más "
                        "solamente por tener mejores especificaciones: "
                        "primero considera qué programas y actividades "
                        "utilizarás."
                    )

                    if mas_caro["existencia"] <= 0:

                        respuesta += (
                            f"\n\nAdemás, {mas_caro['nombre']} "
                            "está agotado actualmente."
                        )

                return jsonify({"respuesta": respuesta})

            # ======================================
            # CONSTRUIR COMPARACIÓN GENERAL
            # ======================================

            respuesta = (
                "COMPARACIÓN DE PRODUCTOS "
                "MUNDO INFORMÁTICO\n\n"
            )

            for numero, producto in enumerate(
                [producto1, producto2], start=1
            ):

                respuesta += (
                    f"{numero}. {producto['nombre']}\n"
                    f"   Categoría: "
                    f"{producto['categoria'] or 'General'}\n"
                    f"   Precio: "
                    f"${float(producto['precio']):,.2f} MXN\n"
                )

                if producto["descripcion"]:

                    respuesta += (
                        f"   Descripción: "
                        f"{producto['descripcion']}\n"
                    )

                datos_tecnicos = obtener_especificaciones(
                    producto
                )

                if datos_tecnicos:

                    respuesta += "   Especificaciones:\n"

                    for dato in datos_tecnicos:
                        respuesta += f"   - {dato}\n"

                if producto["existencia"] > 0:

                    respuesta += (
                        f"   Existencias: "
                        f"{producto['existencia']}\n"
                    )

                else:

                    respuesta += "   Estado: Agotado\n"

                respuesta += "\n"

            # ======================================
            # DIFERENCIA DE PRECIOS
            # ======================================

            respuesta += (
                "DIFERENCIA DE PRECIOS\n\n"
                f"La diferencia entre ambos productos "
                f"es de ${diferencia:,.2f} MXN.\n\n"
            )

            if precio1 > precio2:

                respuesta += (
                    f"{producto1['nombre']} cuesta "
                    f"${diferencia:,.2f} MXN más que "
                    f"{producto2['nombre']}.\n"
                )

            elif precio2 > precio1:

                respuesta += (
                    f"{producto2['nombre']} cuesta "
                    f"${diferencia:,.2f} MXN más que "
                    f"{producto1['nombre']}.\n"
                )

            else:

                respuesta += (
                    "Ambos productos tienen el mismo precio.\n"
                )

            # ======================================
            # COMPARACIÓN DE PROCESADORES
            # ======================================

            if criterio == "procesador":

                cpu1 = normalizar(
                    producto1.get("procesador") or ""
                )

                cpu2 = normalizar(
                    producto2.get("procesador") or ""
                )

                respuesta += (
                    "\nCOMPARACIÓN DE PROCESADORES\n\n"
                    f"{producto1['nombre']}: "
                    f"{producto1.get('procesador') or 'Sin especificar'}\n"
                    f"{producto2['nombre']}: "
                    f"{producto2.get('procesador') or 'Sin especificar'}\n\n"
                )

                intel_i5 = "i5-12450h"
                ryzen_5500 = "ryzen 5 5500u"

                if (
                    intel_i5 in cpu1 and ryzen_5500 in cpu2
                ) or (
                    intel_i5 in cpu2 and ryzen_5500 in cpu1
                ):

                    respuesta += (
                        "El Intel Core i5-12450H generalmente "
                        "ofrece mayor rendimiento de procesamiento "
                        "que el AMD Ryzen 5 5500U en tareas exigentes.\n\n"
                        "El Intel pertenece a la serie H, orientada "
                        "a equipos de mayor rendimiento, mientras "
                        "que el Ryzen 5 5500U pertenece a la serie U, "
                        "orientada a la eficiencia energética.\n\n"
                        "RECOMENDACIÓN:\n"
                        "En rendimiento de procesador, la opción "
                        "más potente entre estas dos configuraciones "
                        "es la que utiliza Intel Core i5-12450H."
                    )

                else:

                    respuesta += (
                        "No tengo una evaluación registrada "
                        "para determinar cuál ofrece mayor "
                        "rendimiento. Es necesario comparar "
                        "los modelos específicos de ambos procesadores."
                    )

            # ======================================
            # COMPARACIÓN DE MEMORIA RAM
            # ======================================

            elif criterio == "ram":

                ram1 = str(
                    producto1.get("memoria_ram") or ""
                )

                ram2 = str(
                    producto2.get("memoria_ram") or ""
                )

                respuesta += "\nCOMPARACIÓN DE MEMORIA RAM\n\n"

                respuesta += (
                    f"{producto1['nombre']}: "
                    f"{ram1 or 'Sin especificar'}\n"
                    f"{producto2['nombre']}: "
                    f"{ram2 or 'Sin especificar'}\n\n"
                )

                valor1 = obtener_capacidad(ram1)
                valor2 = obtener_capacidad(ram2)

                if valor1 is not None and valor2 is not None:

                    if valor1 > valor2:

                        respuesta += (
                            f"{producto1['nombre']} tiene "
                            "mayor capacidad de memoria RAM."
                        )

                    elif valor2 > valor1:

                        respuesta += (
                            f"{producto2['nombre']} tiene "
                            "mayor capacidad de memoria RAM."
                        )

                    else:

                        respuesta += (
                            "Ambos productos tienen la misma "
                            "capacidad de memoria RAM."
                        )

                else:

                    respuesta += (
                        "No hay suficientes datos para "
                        "comparar la capacidad de memoria RAM."
                    )

            # ======================================
            # COMPARACIÓN DE TARJETAS GRÁFICAS
            # ======================================

            elif criterio == "grafica":

                gpu1 = normalizar(
                    producto1.get("tarjeta_grafica") or ""
                )

                gpu2 = normalizar(
                    producto2.get("tarjeta_grafica") or ""
                )

                respuesta += "\nCOMPARACIÓN DE TARJETAS GRÁFICAS\n\n"

                respuesta += (
                    f"{producto1['nombre']}: "
                    f"{producto1.get('tarjeta_grafica') or 'Sin especificar'}\n"
                    f"{producto2['nombre']}: "
                    f"{producto2.get('tarjeta_grafica') or 'Sin especificar'}\n\n"
                )

                if (
                    "rtx 3050" in gpu1 and "integrada" in gpu2
                ) or (
                    "rtx 3050" in gpu2 and "integrada" in gpu1
                ):

                    respuesta += (
                        "La NVIDIA GeForce RTX 3050 es "
                        "considerablemente más adecuada para "
                        "juegos y aplicaciones gráficas exigentes "
                        "que una gráfica integrada AMD Radeon.\n\n"
                        "RECOMENDACIÓN:\n"
                        "La opción con NVIDIA GeForce RTX 3050 "
                        "es la más potente en rendimiento gráfico "
                        "entre estas dos configuraciones."
                    )

                else:

                    respuesta += (
                        "No tengo una evaluación registrada "
                        "para determinar cuál tarjeta gráfica "
                        "ofrece mayor rendimiento."
                    )

            # ======================================
            # COMPARACIÓN DE ALMACENAMIENTO
            # ======================================

            elif criterio == "almacenamiento":

                almacenamiento1 = str(
                    producto1.get("almacenamiento") or ""
                )

                almacenamiento2 = str(
                    producto2.get("almacenamiento") or ""
                )

                respuesta += "\nCOMPARACIÓN DE ALMACENAMIENTO\n\n"

                respuesta += (
                    f"{producto1['nombre']}: "
                    f"{almacenamiento1 or 'Sin especificar'}\n"
                    f"{producto2['nombre']}: "
                    f"{almacenamiento2 or 'Sin especificar'}\n\n"
                )

                capacidad1 = obtener_capacidad(almacenamiento1)
                capacidad2 = obtener_capacidad(almacenamiento2)

                if capacidad1 is not None and capacidad2 is not None:

                    if capacidad1 > capacidad2:

                        respuesta += (
                            f"{producto1['nombre']} tiene "
                            "mayor capacidad de almacenamiento."
                        )

                    elif capacidad2 > capacidad1:

                        respuesta += (
                            f"{producto2['nombre']} tiene "
                            "mayor capacidad de almacenamiento."
                        )

                    else:

                        respuesta += (
                            "Ambos productos tienen la misma "
                            "capacidad de almacenamiento."
                        )

                else:

                    respuesta += (
                        "No hay suficientes datos para "
                        "comparar el almacenamiento."
                    )

            # ======================================
            # COMPARACIÓN DE PANTALLAS
            # ======================================

            elif criterio == "pantalla":

                respuesta += "\nCOMPARACIÓN DE PANTALLAS\n\n"

                respuesta += (
                    f"{producto1['nombre']}: "
                    f"{producto1.get('pantalla') or 'Sin especificar'}\n"
                    f"{producto2['nombre']}: "
                    f"{producto2.get('pantalla') or 'Sin especificar'}\n\n"
                    "Para determinar cuál ofrece mejor calidad "
                    "de imagen se deben comparar resolución, "
                    "frecuencia de actualización y tipo de panel."
                )

            respuesta += (
                "\n\nRecuerda que la disponibilidad puede cambiar. "
                "¿Te gustaría consultar alguno de estos productos?"
            )

            return jsonify({"respuesta": respuesta})

        # ==========================================
        # INFORMACIÓN DETALLADA DE PRODUCTOS
        # ==========================================

        frases_detalle = [
            "hablame de",
            "informacion de",
            "mas informacion",
            "detalles de",
            "detalle de",
            "quiero saber sobre",
            "quiero conocer",
            "describe",
            "descripcion de",
            "caracteristicas de",
            "que caracteristicas tiene",
            "cuales son sus caracteristicas",
            "que especificaciones tiene",
            "cuales son sus especificaciones",
            "cuentame de",
            "cuales son las especificaciones de",
            "cuales son las especificaciones del",
            "cuales son las especificaciones",
            "especificaciones de",
            "especificaciones del",
            "especificaciones tecnicas de",
            "especificaciones tecnicas del",
            "que procesador tiene",
            "que tarjeta grafica tiene",
            "cuanta memoria ram tiene",
            "cuanto almacenamiento tiene",
            "que pantalla tiene",
            "que garantia tiene",
            "que ram tiene",
            "que grafica tiene"
        ]

        es_consulta_detalle = any(
            frase in mensaje_normalizado
            for frase in frases_detalle
        )

        if es_consulta_detalle:

            cursor.execute(
                consulta_base + " ORDER BY p.nombre ASC"
            )

            catalogo = cursor.fetchall()

            productos_encontrados = []

            for producto in catalogo:

                nombre_normalizado = normalizar(
                    producto["nombre"]
                )

                patron = (
                    r"(?<!\w)"
                    + re.escape(nombre_normalizado)
                    + r"(?!\w)"
                )

                if re.search(patron, mensaje_normalizado):

                    productos_encontrados.append(producto)

            productos_encontrados.sort(
                key=lambda producto: len(
                    normalizar(producto["nombre"])
                ),
                reverse=True
            )

            if productos_encontrados:

                producto = productos_encontrados[0]

                respuesta = (
                    "INFORMACIÓN DEL PRODUCTO\n\n"
                    f"Nombre: {producto['nombre']}\n"
                    f"Categoría: "
                    f"{producto['categoria'] or 'General'}\n"
                    f"Precio: "
                    f"${float(producto['precio']):,.2f} MXN\n"
                )

                if producto["descripcion"]:

                    respuesta += (
                        f"Descripción: "
                        f"{producto['descripcion']}\n"
                    )

                else:

                    respuesta += "Descripción: No disponible.\n"

                datos_tecnicos = obtener_especificaciones(
                    producto
                )

                if datos_tecnicos:

                    respuesta += "\nESPECIFICACIONES TÉCNICAS\n"

                    for dato in datos_tecnicos:
                        respuesta += f"{dato}\n"

                else:

                    respuesta += (
                        "\nESPECIFICACIONES TÉCNICAS\n"
                        "Todavía no se han registrado "
                        "especificaciones para este producto.\n"
                    )

                if producto["existencia"] > 0:

                    respuesta += (
                        f"\nExistencias disponibles: "
                        f"{producto['existencia']}\n"
                    )

                else:

                    respuesta += (
                        "\nEstado: Actualmente agotado.\n"
                    )

                respuesta += (
                    "\n¿Te gustaría consultar otro producto?"
                )

            else:

                respuesta = (
                    "No pude identificar el producto que "
                    "deseas consultar. Escribe su nombre "
                    "tal como aparece en nuestro catálogo."
                )

            return jsonify({"respuesta": respuesta})

        # ==========================================
        # DETECTAR PRESUPUESTO
        # ==========================================

        coincidencia_presupuesto = re.search(
            r"(?:menos\s+de|no\s+mas\s+de|por\s+debajo\s+de|"
            r"maximo(?:\s+de)?|hasta|con|presupuesto(?:\s+de)?)"
            r"\s*\$?\s*([\d,]+(?:\.\d{1,2})?)",
            mensaje_normalizado
        )

        if coincidencia_presupuesto:

            presupuesto = float(
                coincidencia_presupuesto.group(1).replace(",", "")
            )

            if presupuesto <= 0:

                return jsonify({
                    "respuesta": "Indica un presupuesto mayor a cero."
                })

            filtros_ignorados = {
                "comprar", "compro", "cuestan", "presupuesto",
                "tengo", "alcanza", "alcanzar", "maximo",
                "menos", "hasta", "debajo", "dinero",
                "pesos", "mxn", "puedo"
            }

            filtros = [
                palabra for palabra in palabras
                if not palabra.isdigit()
                and palabra not in filtros_ignorados
            ]

            consulta = consulta_base + """
                AND p.precio <= %s
                AND p.existencia > 0
            """

            parametros = [presupuesto]

            if filtros:

                condiciones = []

                for palabra in filtros:

                    condiciones.append("""
                        (
                            LOWER(p.nombre) LIKE %s
                            OR LOWER(COALESCE(p.descripcion, '')) LIKE %s
                            OR LOWER(COALESCE(c.nombre, '')) LIKE %s
                        )
                    """)

                    busqueda = f"%{palabra}%"

                    parametros.extend([
                        busqueda,
                        busqueda,
                        busqueda
                    ])

                consulta += (
                    " AND (" + " AND ".join(condiciones) + ")"
                )

            consulta += """
                ORDER BY p.precio ASC
                LIMIT 5
            """

            cursor.execute(consulta, parametros)
            productos = cursor.fetchall()

            if productos:

                respuesta = (
                    "Encontré estos productos disponibles "
                    f"por un máximo de ${presupuesto:,.2f} MXN:\n\n"
                )

                for producto in productos:

                    respuesta += (
                        f"• {producto['nombre']}\n"
                        f"  Categoría: "
                        f"{producto['categoria'] or 'General'}\n"
                        f"  Precio: "
                        f"${float(producto['precio']):,.2f} MXN\n"
                        f"  Existencias: {producto['existencia']}\n\n"
                    )

                respuesta += (
                    "¿Te gustaría conocer más detalles "
                    "de alguno de estos productos?"
                )

            else:

                respuesta = (
                    "No encontré productos disponibles que "
                    "coincidan con tu búsqueda y presupuesto "
                    f"de ${presupuesto:,.2f} MXN."
                )

            return jsonify({"respuesta": respuesta})

        # ==========================================
        # BÚSQUEDA NORMAL
        # ==========================================

        if palabras:

            condiciones = []
            parametros = []

            for palabra in palabras:

                condiciones.append("""
                    (
                        LOWER(p.nombre) LIKE %s
                        OR LOWER(COALESCE(p.descripcion, '')) LIKE %s
                        OR LOWER(COALESCE(c.nombre, '')) LIKE %s
                    )
                """)

                busqueda = f"%{palabra}%"

                parametros.extend([
                    busqueda,
                    busqueda,
                    busqueda
                ])

            consulta = consulta_base + """
                AND (""" + " OR ".join(condiciones) + """)
                ORDER BY
                    p.existencia DESC,
                    p.nombre ASC
                LIMIT 5
            """

            cursor.execute(consulta, parametros)

        else:

            consulta = consulta_base + """
                ORDER BY
                    p.existencia DESC,
                    p.nombre ASC
                LIMIT 5
            """

            cursor.execute(consulta)

        productos = cursor.fetchall()

        # ==========================================
        # PREPARAR RESPUESTA
        # ==========================================

        if productos:

            respuesta = (
                "Encontré estos productos en "
                "MUNDO INFORMÁTICO:\n\n"
            )

            for producto in productos:

                precio = float(producto["precio"])

                respuesta += (
                    f"• {producto['nombre']}\n"
                    f"  Categoría: "
                    f"{producto['categoria'] or 'General'}\n"
                    f"  Precio: ${precio:,.2f} MXN\n"
                )

                if producto["existencia"] > 0:

                    respuesta += (
                        f"  Existencias: "
                        f"{producto['existencia']}\n"
                    )

                else:

                    respuesta += "  Estado: Agotado\n"

                respuesta += "\n"

            respuesta += (
                "¿Te gustaría conocer más detalles "
                "de alguno de estos productos?"
            )

        else:

            respuesta = (
                "No encontré productos que coincidan "
                "con tu búsqueda. Puedes intentar con "
                "otro nombre o categoría."
            )

        return jsonify({"respuesta": respuesta})

    except Exception:

        app.logger.exception("Error en el chatbot")

        return jsonify({
            "respuesta": (
                "Lo siento, ocurrió un problema al consultar "
                "el catálogo. Inténtalo nuevamente."
            )
        }), 500

    finally:

        if cursor:
            cursor.close()

        if conexion and conexion.is_connected():
            conexion.close()


if __name__ == "__main__":

    app.run(debug=True)
