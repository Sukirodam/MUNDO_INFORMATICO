
import os
from dotenv import load_dotenv
from openai import OpenAI

# Cargar las variables del archivo .env
load_dotenv()

API_KEY = os.getenv("OPENAI_API_KEY")
MODELO = os.getenv("OPENAI_MODEL", "gpt-5.5")

# Crear el cliente de OpenAI
cliente = OpenAI(api_key=API_KEY) if API_KEY else None


def generar_respuesta(mensaje, contexto_tienda="", historial=None):

    if cliente is None:
        raise RuntimeError("La API KEY de OpenAI no está configurada.")

    instrucciones = """
    Eres el asistente virtual oficial de MUNDO INFORMÁTICO,
    una tienda de productos tecnológicos.

    Tu función es ayudar a los clientes de manera amable,
    clara y profesional.

    REGLAS IMPORTANTES:

    1. No inventes productos, precios ni existencias.
    2. Utiliza únicamente los datos proporcionados
       en el contexto de la tienda.
    3. Si no tienes información suficiente, dilo claramente.
    4. Puedes explicar temas de informática y tecnología.
    5. Puedes comparar productos cuando tengas sus datos.
    6. No inventes promociones ni métodos de pago.
    7. Responde en español.
    8. Utiliza respuestas fáciles de entender.
    9. Nunca solicites contraseñas ni datos bancarios.
    """

    entrada = []

    # Agregar historial de conversación
    if historial:
        for elemento in historial[-8:]:
            if elemento.get("role") in ("user", "assistant"):
                entrada.append({
                    "role": elemento["role"],
                    "content": elemento["content"]
                })

    # Agregar la pregunta actual y los datos de la tienda
    contenido = f"""
    INFORMACIÓN ACTUAL DE LA TIENDA:

    {contexto_tienda}

    PREGUNTA DEL CLIENTE:

    {mensaje}
    """

    entrada.append({
        "role": "user",
        "content": contenido
    })

    respuesta = cliente.responses.create(
        model=MODELO,
        instructions=instrucciones,
        input=entrada,
        max_output_tokens=500
    )

    return respuesta.output_text
