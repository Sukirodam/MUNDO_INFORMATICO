
from db import obtener_conexion


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
            return "Actualmente no hay productos disponibles en el catálogo."

        contexto = "CATÁLOGO ACTUAL DE MUNDO INFORMÁTICO:\n"

        for producto in productos:
            contexto += f"""
            Producto: {producto['nombre']}
            Categoría: {producto['categoria'] or 'Sin categoría'}
            Descripción: {producto['descripcion'] or 'Sin descripción'}
            Precio: ${producto['precio']} MXN
            Existencias: {producto['existencia']}
            -------------------------
            """

        return contexto

    except Exception as error:
        print("Error al consultar el catálogo:", error)
        return "No fue posible consultar el catálogo en este momento."

    finally:
        if cursor:
            cursor.close()

        if conexion and conexion.is_connected():
            conexion.close()
