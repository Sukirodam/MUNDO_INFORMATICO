
from db import obtener_conexion
from decimal import Decimal


# ==========================================
# OBTENER INFORMACIÓN DE LA TIENDA
# ==========================================

def obtener_contexto_tienda():

    conexion = None
    cursor = None

    try:

        conexion = obtener_conexion()
        cursor = conexion.cursor(dictionary=True)

        consulta = """
            SELECT
                p.id_producto,
                p.nombre,
                p.descripcion,
                p.precio,
                p.existencia,
                c.nombre AS categoria
            FROM productos p
            LEFT JOIN categorias c
                ON p.id_categoria = c.id_categoria
            WHERE p.activo = 1
            ORDER BY p.nombre ASC
            LIMIT 20
        """

        cursor.execute(consulta)
        productos = cursor.fetchall()

        if not productos:
            return (
                "El catálogo de MUNDO INFORMÁTICO "
                "no tiene productos activos actualmente."
            )

        lineas = [
            "CATÁLOGO ACTUAL DE MUNDO INFORMÁTICO",
            "Información obtenida directamente de MySQL.",
            "Los precios están expresados en pesos mexicanos.",
            ""
        ]

        for producto in productos:

            precio = Decimal(str(producto["precio"]))

            existencia = int(producto["existencia"] or 0)

            disponibilidad = (
                "Disponible"
                if existencia > 0
                else "Agotado"
            )

            lineas.append(
                f"""
ID del producto: {producto['id_producto']}
Nombre: {producto['nombre']}
Categoría: {producto['categoria'] or 'Sin categoría'}
Descripción: {producto['descripcion'] or 'Sin descripción'}
Precio: ${precio:,.2f} MXN
Existencias: {existencia}
Disponibilidad: {disponibilidad}
------------------------------
"""
            )

        return "\n".join(lineas)

    except Exception as error:

        print(
            "Error al consultar el catálogo:",
            error
        )

        raise RuntimeError(
            "No fue posible consultar el catálogo de la tienda."
        ) from error

    finally:

        if cursor:
            cursor.close()

        if conexion and conexion.is_connected():
            conexion.close()
