# Written by ChatGPT 5.3 mini

from fastapi import FastAPI, HTTPException, Response
import httpx
import numpy as np
from io import BytesIO
from PIL import Image
import rasterio
from rasterio.transform import from_bounds
import mercantile
import asyncio
from concurrent.futures import ThreadPoolExecutor

app = FastAPI()

TILE_URL = 'https://tiles.mapterhorn.com/{z}/{x}/{y}.webp'

HTTP_CONCURRENCY = 50
CPU_WORKERS = 8

http_semaphore = asyncio.Semaphore(HTTP_CONCURRENCY)
executor = ThreadPoolExecutor(max_workers=CPU_WORKERS)

client = httpx.AsyncClient(
    timeout=10,
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
)


def terrarium_to_elevation(img: np.ndarray) -> np.ndarray:
    r = img[:, :, 0].astype(np.float32)
    g = img[:, :, 1].astype(np.float32)
    b = img[:, :, 2].astype(np.float32)
    return (r * 256.0 + g + b / 256.0) - 32768.0


async def fetch_tile(z, x, y):
    url = TILE_URL.format(z=z, x=x, y=y)

    async with http_semaphore:
        r = await client.get(url)

    if r.status_code != 200:
        return None

    return r.content


def decode_tile(content: bytes):
    img = Image.open(BytesIO(content)).convert('RGB')
    arr = np.array(img)
    return terrarium_to_elevation(arr).astype(np.float32)


def build_geotiff(elev, bounds):
    transform = from_bounds(
        bounds.left,
        bounds.bottom,
        bounds.right,
        bounds.top,
        elev.shape[1],
        elev.shape[0],
    )

    memfile = BytesIO()

    with rasterio.MemoryFile() as mem:
        with mem.open(
            driver='GTiff',
            height=elev.shape[0],
            width=elev.shape[1],
            count=1,
            dtype='float32',
            crs='EPSG:3857',
            transform=transform,
        ) as dst:
            dst.write(elev, 1)

        memfile.write(mem.read())
        memfile.seek(0)

    return memfile.read()


@app.get('/{z}/{x}/{y}.tif')
async def get_tile(z: int, x: int, y: int):

    content = await fetch_tile(z, x, y)
    if content is None:
        raise HTTPException(status_code=404, detail='Tile not found')

    elev = await asyncio.get_event_loop().run_in_executor(
        executor, decode_tile, content
    )

    bounds = mercantile.xy_bounds(x, y, z)

    tif_bytes = await asyncio.get_event_loop().run_in_executor(
        executor, build_geotiff, elev, bounds
    )

    return Response(content=tif_bytes, media_type='image/tiff')