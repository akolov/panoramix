import json
import logging
import lzma
import os
import shutil
import threading
from pathlib import Path
from sqlite3 import DatabaseError, OperationalError

from panoramix.utils.db import get_sqlite3_cursor
from panoramix.utils.helpers import cache_dir, cached

"""
    a module for management of bytes4 signatures from the database

     db schema:

     hash - 0x12345678
     name - transferFrom
     folded_name - transferFrom(address, address, uint256)
     cooccurs - comma-delimited list of hashes: `0x12312312,0xabababab...`
     params - json: `[
            {
              "type": "address",
              "name": "_from"
            },
            {
              "type": "address",
              "name": "_to"
            },
            {
              "type": "uint256",
              "name": "_value"
            }
          ]`

"""

logger = logging.getLogger(__name__)
_lock = threading.Lock()


def supplements_path():
    if decompressed_supplements_path().is_file:
        return decompressed_supplements_path()
    else:
        return cache_dir(False) / "supplement.db"


def decompressed_supplements_path():
    return Path(__file__).parent.parent / "data" / "supplement.db"


def compressed_supplements_path():
    return Path(__file__).parent.parent / "data" / "supplement.db.xz"


def decompress_supplements():
    compressed = compressed_supplements_path()
    decompressed = decompressed_supplements_path()
    with _lock:
        logger.info("Decompressing %s into %s...", compressed, decompressed)
        if not decompressed.is_file():
            with lzma.open(compressed) as inf, decompressed.open("wb") as outf:
                while buf := inf.read(1024 * 1024):
                    outf.write(buf)

        c = get_sqlite3_cursor(decompressed)
        try:
            c.execute("SELECT COUNT(1) FROM functions")
        except (DatabaseError, OperationalError):
            logger.exception("Could not properly decompress supplements")
            os.remove(decompressed)


def check_supplements():
    panoramix_supplements = supplements_path()

    with _lock:
        if panoramix_supplements.is_file():
            c = get_sqlite3_cursor(panoramix_supplements)
            try:
                c.execute("SELECT COUNT(1) FROM functions")
            except (DatabaseError, OperationalError):
                logger.exception("Invalid supplements")
                os.remove(panoramix_supplements)

        if not panoramix_supplements.is_file():
            decompressed_supplements = decompressed_supplements_path()
            if decompressed_supplements.is_file():
                logger.info("Copying %s into %s...", decompressed_supplements, panoramix_supplements)
                shutil.copy(decompressed_supplements, panoramix_supplements)
            else:
                compressed_supplements = compressed_supplements_path()
                logger.info("Decompressing %s into %s...", compressed_supplements, panoramix_supplements)
                with lzma.open(compressed_supplements) as inf, panoramix_supplements.open("wb") as outf:
                    while buf := inf.read(1024 * 1024):
                        outf.write(buf)

        assert panoramix_supplements.is_file()


def _cursor():
    check_supplements()
    return get_sqlite3_cursor(supplements_path())


@cached
def fetch_sigs(hash):
    c = _cursor()
    c.execute("SELECT * from functions where hash=?", (hash,))

    results = c.fetchall()

    res = []
    for row in results:
        res.append(
            {
                "hash": row[0],
                "name": row[1],
                "folded_name": row[2],
                "params": json.loads(row[3]),
                "cooccurs": row[4].split(","),
            }
        )

    return res


@cached
def fetch_sig(hash):
    if type(hash) == str:
        hash = int(hash, 16)
    hash = "{:#010x}".format(hash)

    c = _cursor()
    c.execute(
        "SELECT hash, name, folded_name, params, cooccurs from functions where hash=?",
        (hash,),
    )

    results = c.fetchall()
    if len(results) == 0:
        return None

    # Take the one that cooccurs with the most things, it's probably the most relevant.
    row = max(results, key=lambda row: len(row[4]))

    return {
        "hash": hash,
        "name": row[1],
        "folded_name": row[2],
        "params": json.loads(row[3]),
    }


"""

    Abi crawler and parser. used to refill supplement.py with new ABI/func definitions.
    It's used by scripts that are not a part of panoramix repo.

    The function is here, so people wanting to parse ABIs on their own can use parse_insert_abi
    implementation as a reference. It handles some unobvious edge-cases, like arrays of tuples.

"""


def crawl_abis_from_cache():
    # imports here, because this is not used as a part of a regular panoramix run,
    # and we don't want to import stuff unnecessarily.

    import json
    import os
    import re
    import sqlite3
    import sys
    import time
    import urllib
    import urllib.request

    try:
        from web3 import Web3
    except Exception:
        print("install web3:\n\t`pip install web3`")  # the only dependency in the project :D

    conn = sqlite3.connect("supplement.db")
    cursor = conn.cursor()

    conn2 = sqlite3.connect("supp2.db")
    cursor2 = conn2.cursor()

    def parse_insert_abi(abi):
        def parse_inputs(func_inputs):
            inputs = []
            params = []
            param_counter = 0
            for r in func_inputs:
                param_counter += 1
                type_ = r["type"]

                name_ = r["name"]
                if len(name_) == 0:
                    name_ = "param" + str(param_counter)

                if name_[0] != "_":
                    name_ = "_" + name_

                params.append({"type": r["type"], "name": name_})

                if "tuple" not in type_:
                    inputs.append(type_)
                else:
                    type_ = f"({parse_inputs(r['components'])[0]})" + type_[5:]
                    inputs.append(type_)

            return ",".join(inputs), params

        output = {}

        for func in abi:
            if func["type"] in ["constructor", "fallback"]:
                continue

            inputs, params = parse_inputs(func["inputs"])

            fname = f"{func['name']}({inputs})"

            sha3 = Web3.sha3(text=fname).hex()[:10]

            if sha3 in output:
                print("double declaration for the same hash! {}".format(fname))
                continue

            output[sha3] = {
                "name": func["name"],
                "folded_name": fname,
                "params": params,
            }

        for sha3, row in output.items():
            row["cooccurs"] = list(output.keys())
            insert_row = (
                sha3,
                row["name"],
                row["folded_name"],
                json.dumps(row["params"]),
                ",".join(row["cooccurs"]),
            )

            insert_row2 = (
                int(sha3, 16),
                row["name"],
                row["folded_name"],
                json.dumps(row["params"]),
            )

            test_hash, test_cooccurs = insert_row[0], insert_row[4]

            cursor.execute(
                "SELECT * from functions where hash=? and cooccurs=?",
                (test_hash, test_cooccurs),
            )
            results = cursor.fetchall()
            if len(results) == 0:
                print("inserting", sha3, row["folded_name"])
                cursor.execute("INSERT INTO functions VALUES (?, ?, ?, ?, ?)", insert_row)
                conn.commit()

            cursor2.execute("SELECT * from functions where hash=?", (insert_row2[0],))
            results = cursor2.fetchall()
            if len(results) == 0:
                print("inserting2", sha3, row["folded_name"])
                cursor2.execute("INSERT INTO functions VALUES (?, ?, ?, ?)", insert_row2)

                conn2.commit()

    def crawl_cache():
        idx = 0

        path = "./cache_abis/"

        if not os.path.isdir(path):
            print("dir cache_abis doesn't exist. it should be there and it should contain abi files")
            return

        for fname in os.listdir(path):
            address = fname[:-4]
            fname = path + fname

            idx += 1
            print(idx, address)

            with open(fname) as f:
                abi = json.loads(f.read())
                parse_insert_abi(abi)

    crawl_cache()
