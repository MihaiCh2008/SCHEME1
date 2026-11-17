"""Solana wallets labeled as centralized exchanges (CEX). Their holdings are left out of holder concentration.

An exchange wallet holds coins for many customers, who don't sell at the same moment, so it isn't the
"few wallets could dump on the pool" risk the check is about (DECISIONS.md D21).

Source: Dune Analytics Spellbook, commit 475a7e1db901aeb67539bfa701033eb9b32dcd79, file
dbt_subprojects/hourly_spellbook/models/_sector/cex/addresses/chains/solana/cex_solana_addresses.sql,
read 2026-11-17: 112 addresses, each checked to be a valid 32-byte Solana address.

License: this list is a copy of part of Spellbook and stays under Spellbook's Business Source License 1.1,
in LICENSE-dune-spellbook.txt next to this file (it allows copying, redistributing and using it, except to
offer a data or analytics platform to third parties). The rest of SCHEME1 keeps its own license.

To update: read the newest version of that file and regenerate this dict the same way. Add an address
from another source only with the source written next to it; a whale wrongly listed here would get past
the holder check.
"""

EXCHANGE_WALLETS: dict[str, str] = {
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": "Binance 1",  # added 2024-04-19
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "Binance 2",  # added 2024-04-19
    "Amf2mf2Ciap5wYAEKDtGoQHfHPWaKbyFYhutMK46hTRF": "Binance 3",  # added 2024-04-19
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S": "Binance 4",  # added 2024-04-19
    "DRpbCBMxVnDK7maPM5tGv6MvB3v1sRMC86PZ8okm21hy": "Binance 5",  # added 2024-04-19
    "G9RCBaYb8aBRxoe8QBC2ucGrVqjuZFysRhY8d56cnNT1": "Binance 8",  # added 2025-07-07
    "HXsKP7wrBWaQ8T2Vtjry3Nj3oUgwYcqq9vrHDM12G664": "Binance 9",  # added 2024-04-19
    "Edbef6Xi35u6iYEmeGAFmThCZrgj6MxzYAgAmZFYPmFF": "Binance 17",  # added 2025-07-07
    "3gd3dqgtJ4jWfBfLYTX67DALFetjc5iS72sCgRhCkW2u": "Binance 18",  # added 2024-04-19
    "3yFwqXBfZY4jBVUafQ1YEXw189y2dN3V5KQq9uzBDy1E": "Binance 19",  # added 2024-04-19
    "6QJzieMYfp7yr3EdrePaQoG3Ghxs2wM98xSLRu8Xh56U": "Binance 20",  # added 2024-04-19
    "GBrURzmtWujJRTA3Bkvo7ZgWuZYLMMwPCwre7BejJXnK": "Binance 21",  # added 2025-07-07
    "6oCa9Tz8VXVp63WiFyruE5PD6yXz3pCsv6oGzUGvg9TP": "Binance 22",  # added 2025-07-07
    "GK35nWN6ZHSGZrRTf8kTQd8RkFCighChPEb41XwSFVAC": "Binance 23",  # added 2025-07-07
    "EPauhjQjjTBCpeBtszS3xGRASLpEJFM1cspSiFRXZa9Z": "Binance 24",  # added 2025-07-07
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "Coinbase 1",  # added 2024-04-19
    "2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm": "Coinbase 2",  # added 2024-04-19
    "GJRs4FwHtemZ5ZE9x3FNvJ8TMwitKTh21yxdRPqn7npE": "Coinbase 3",  # added 2024-04-19
    "6FEVkH17P9y8Q9aCkDdPcMDjvj7SVxrTETaYEm8f51Jy": "Crypto.com 1",  # added 2024-04-19
    "AobVSwdW9BbpMdJvTqeCN4hPAmh4rHm7vwLnQ5ATSyrS": "Crypto.com 2",  # added 2024-04-19
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5": "Kraken 1",  # added 2024-04-19
    "krakeNd6ednDPEXxHAmoBs1qKVM8kLg79PvWF2mhXV1": "Kraken 2",  # added 2024-04-19
    "ASTyfSima4LLAdDgoFGkgqoKowG1LZFDr9fAQrg7iaJZ": "MEXC 1",  # added 2024-04-19
    "57vSaRTqN9iXaemgh4AoDsZ63mcaoshfMK8NP3Z5QNbs": "KuCoin 1",  # added 2024-04-19
    "BmFdpraQhkiDQE6SnfG5omcA1VwzqfXrwtNYBwWTymy6": "KuCoin 2",  # added 2024-04-19
    "HVh6wHNBAsG3pq1Bj5oCzRjoWKVogEDHwUHkRz3ekFgt": "KuCoin 3",  # added 2024-04-19
    "EkUy8BB574iEVAQE9dywEiMhp9f2mFBuFu6TBKAkQxFY": "KuCoin 5",  # added 2024-04-19
    "6b4aypBhH337qSzzkbeoHWzTLt4DjG2aG8GkrrTQJfQA": "FTX 1",  # added 2024-04-19
    "6wEMcwrcF5AP9jpHWQcPxHXciWA2g217Qq81CTWjbgBw": "FTX 2",  # added 2024-04-19
    "9uyDy9VDBw4K7xoSkhmCAm8NAFCwu4pkF6JeHUCtVKcX": "FTX 3",  # added 2024-04-19
    "6ZRCB7AAqGre6c72PRz3MHLC73VMYvJ8bi9KHf1HFpNk": "FTX 4",  # added 2024-04-19
    "JBpj7yp4Afvb71TmanVwJZXGeX4kqbGFvjCFCRo3EbTM": "FTX US 1",  # added 2024-04-19
    "FxteHmLwG9nk1eL4pjNve3Eub2goGkkz6g6TbvdmW46a": "Bitfinex 9",  # added 2024-04-19
    "J4rzLDLhLWFpjSgCMCcxTU84bQ8AH5vhgjwq7SjYVk8Q": "Bitfinex 10",  # added 2024-04-19
    "FyJBKcfcEBzGN74uNxZ95GxnCxeuJJujQCELpPv14ZfN": "Bitfinex 11",  # added 2024-04-19
    "GnCRxKqUEPouYMvTb5nJMGrDB3VkTXZnDTaDuVZdnWA3": "Bitfinex 12",  # added 2024-04-19
    "4DgunMfBb19GaMZ1Z48oqcymZ4eBA4v5SUdRnapzj66G": "Coinsquare 2",  # added 2024-04-19
    "Cet3t77x2BBVSmiEFm8ZPoDSngbpso2RuWPL79Ky7SpA": "Swissborg 1",  # added 2024-04-19
    "Fe7SEekiKygziaEGKxsDsgLVzrCfNvVBvAYsaJBwFA8s": "Swissborg 2",  # added 2024-04-19
    "2XxP4kS2vfkiMvpLpGNxry3fPUYimsuAmSbqL1KnuwZ8": "Swissborg 3",  # added 2024-04-19
    "9qoUcyhKSWMbk6tqGUYQUpeosPcdUnJszG4eQKwfe4gL": "Swissborg 4",  # added 2024-04-19
    "2E1UKoiiZPwsp4vn6tUh5k61kG2UqYpT7oBrFaJUJXXd": "Swissborg 5",  # added 2024-04-19
    "7SRyi7urg28j1XtioKbcJLtkL9Pqu39Y4CTAXwmu2Hts": "CoinList 1",  # added 2024-04-19
    "821dTn3JEhhqg11Q24ijxRBYXfKWngGMibTqHS8hWjZb": "Rollbit 1",  # added 2024-04-19
    "6iVBAsquJRaLsXbojb18kqTW1d5iVLspVjCtsReZBKhY": "AscendEX 1",  # added 2024-04-19
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm": "Backpack 1",  # added 2024-04-19
    "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3": "Backpack 2",  # added 2024-04-19
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "Backpack 3",  # added 2024-04-19
    "hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux": "Backpack 4",  # added 2024-04-19
    "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL": "Backpack 5",  # added 2024-04-19
    "mb1eu7TzEc71KxDpsmsKoucSSuuoGLv1drys1oP2jh6": "Backpack 6",  # added 2024-04-19
    "rndrizKT3MK1iimdxRdWabcF7Zg7AR5T4nud4EkHBof": "Backpack 7",  # added 2024-04-19
    "BbHG9GvPActFGogv3iNrpDAj4qpXr8t3jF16uGxXcKci": "Backpack 8",  # added 2024-04-19
    "43DbAvKxhXh1oSxkJSqGosNw3HpBnmsWiak6tB5wpecN": "Backpack 9",  # added 2024-04-19
    "A77HErqtfN1hLLpvZ9pCtu66FEtM8BveoaKbbMoZ4RiR": "Bitget 1",  # added 2024-04-19
    "E1EfU3iKUS16mB2vdgj6mi3ssV32dm2Pxh7W74XxKuYu": "Bitstamp 1",  # added 2024-04-19
    "ETZY5TjMKdV2KdHVmUNTN56pWhMc8TyjrXtQ7YexDCmG": "Bitstamp 2",  # added 2024-04-19
    "HBxZShcE86UMmF93KUM8eWJKqeEXi5cqWCLYLMMhqMYm": "Bitstamp 3",  # added 2024-04-19
    "i57ExrKB2i4mSgjSuq2xz617mQXmu33WG2WEYypmdvX": "Bitstamp 5",  # added 2024-04-19
    "42brAgAVNzMBP7aaktPvAmBSPEkehnFQejiZc53EpJFd": "Bybit 1",  # added 2024-04-19
    "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2": "Bybit 2",  # added 2024-04-19
    "3Nvb6UeEMVLf9DsehhqT8B5SNHrNLEpHdf42nXi9aLK8": "CoinDCX 2",  # added 2024-04-19
    "4E73T5Zubx3kPtheJ5DB6QMEaadMQowhUcdRV6f9rvVt": "CoinDCX 3",  # added 2024-04-19
    "5bjZPeHbdGUCduyY8i2dZkkgLWkG3u1P2sbt1Jb5kFxW": "CoinDCX 4",  # added 2024-04-19
    "6XuYJv4PyauP2WKdgd4fZYvjK7uBdVkEySoy89fXV9XT": "CoinDCX 5",  # added 2024-04-19
    "6fuHoq5Tr2j6dFTGWHmLUhVBUj8XvDBgdGGzgfSwe5DN": "CoinDCX 6",  # added 2024-04-19
    "6p8LxMvHUZtdiik28TQy483WaCaDGsAUFm3PKjpUpLB": "CoinDCX 7",  # added 2024-04-19
    "8W4u11KJYhmCY9rs2MCVHmMrgpeHLgsbkwAarbNvzr2Y": "CoinDCX 8",  # added 2024-04-19
    "8nFGjbJNdarBouEC856Qmhrm8fptuEUnZrpNKgougsLi": "CoinDCX 9",  # added 2024-04-19
    "8zSWFy8wvHEuFJ7CZ81wy2wn4fPaju6jc6tbtbv5x9f8": "CoinDCX 10",  # added 2024-04-19
    "92qQCjprXoJ5GYji5it8PJTneBFxzSJ6mYosQWxYKxx4": "CoinDCX 11",  # added 2024-04-19
    "9buzBcTujdQLyf1XkyLaSrGNbkyNxg8qML3bpKWCz9P6": "CoinDCX 12",  # added 2024-04-19
    "AjCHG7CYJST379Eh7xLqhRaUyBzurFKjsX7z8DTrfGZp": "CoinDCX 13",  # added 2024-04-19
    "Bq6NnW55ToJrAM7NxUYDmSMbKKCQ6f742ZemLiypqjzY": "CoinDCX 14",  # added 2024-04-19
    "EUXVDPCwXe9sJt3VucYfveB1kk5piSWJqhWQEsFtE2CR": "CoinDCX 15",  # added 2024-04-19
    "EhZLSqyWrazhqR5bmyyEfFRa3ycWu9KPgQgQ7G1R9zC9": "CoinDCX 16",  # added 2024-04-19
    "HwgxMWxaq5cDbdVmoH24Gy8F9m9DkU7Mwgdbgh4mh2m7": "CoinDCX 17",  # added 2024-04-19
    "CLNEVwuSAiGsvPtE74yLhda4beNfd8qfZXVKkUcAJZDL": "Gate.io 1",  # added 2024-04-19
    "u6PJ8DtQuPFnfmwHbGFULQ4u4EgjDiyYKjVEsynXq2w": "Gate.io 6",  # added 2024-04-19
    "HiRpdAZifEsZGdzQ5Xo5wcnaH3D2Jj9SoNsUzcYNK78J": "Gate.io 7",  # added 2024-04-19
    "5bJcc9eb2XE7mqcET2xDuAdMGuXWybb4YPmAHLjKLhQG": "HTX 1",  # added 2024-04-19
    "88xTWZMeKfiTgbfEmPLdsUCQcZinwUfk25EBQZ21XMAZ": "HTX 2",  # added 2024-04-19
    "8NBEbxLknGv5aRYefFrW2qFXoDZyi9fSHJNiJRvEcMBE": "HTX 3",  # added 2024-04-19
    "BY4StcU9Y2BpgH8quZzorg31EGE4L1rjomN8FNsCBEcx": "HTX 4",  # added 2024-04-19
    "51AASorYCLPcUHnuQQaau6DfsfFRixzh4HsoQwsc5Ara": "LAToken 2",  # added 2024-04-19
    "6Attw9NcFspCAreufvQ3mW8aqXEk6MDceeoC8obw45cJ": "OKX 3",  # added 2024-04-19
    "6NnzUC7mrM7ZgKdcMq1jPAdqh9TMgdsaVR1DNq6kce3A": "OKX 4",  # added 2024-04-19
    "7CtzEcGeYpMfR1aEi1Gbt8GRHDPH9uDB6GNTsBABqSmo": "OKX 5",  # added 2024-04-19
    "7fGw3UURsxk1szQ4buxQyEkiF4P6z7vx7sN1MHEguTJg": "OKX 6",  # added 2024-04-19
    "7o9ukGhWvc71yYcjinKnrxMn2kFMyQD1iCG3romrzg7r": "OKX 7",  # added 2024-04-19
    "8dUc88Nss8uhqzzFvUQhepkwZaVrfzpoCfKjCXGkdzAG": "OKX 8",  # added 2024-04-19
    "945SJTwsBSqwEEgtKMnhcLDnJowP6YpJUTEykMVK5k6q": "OKX 9",  # added 2024-04-19
    "9un5wqE3q4oCjyrDkwsdD48KteCJitQX5978Vh7KKxHo": "OKX 10",  # added 2024-04-19
    "APJmXrtC9TUAg5gcjbcsVUiVzeDv85xLZRLad1GiQTNE": "OKX 11",  # added 2024-04-19
    "BEaiMhcc4Kao7B4hoq6r9m8neUpfimUMwhzugKxtkZw6": "OKX 12",  # added 2024-04-19
    "BWZqi67kXsvf5crjd19Fb3gmf3DCipGE8eWWJT2YHDXQ": "OKX 14",  # added 2024-04-19
    "BhJrLQEyFyrcf1746pHATzAZQpC1uk3SJf2AHHK7LW3K": "OKX 15",  # added 2024-04-19
    "CE8joA143dBjsCFTeBxBs3kNGNU87WH6Q7GMMfxPzSr2": "OKX 16",  # added 2024-04-19
    "CGZAHxBannZzYK3rzVdG1e3uNjMxrmaXDhLEZxQBbueq": "OKX 17",  # added 2024-04-19
    "CeGyfZdtbjzC5FeVXCBdYE1v397yxxqYgmMcUwtouJUu": "OKX 18",  # added 2024-04-19
    "EBVigEhxvkWUwreG98kTh5FbZEjnXTXC5ANZtLz4YpHQ": "OKX 19",  # added 2024-04-19
    "FJS2rTjAQmFFRmVqtnkceGvLJrMwUPgCUfLjciuMYkSC": "OKX 20",  # added 2024-04-19
    "FefAbVt2EgXMGxeJb6sB1k5pQJhiGPxw4mB1zeUohZLk": "OKX 21",  # added 2024-04-19
    "GcFZHQVJ2icPGLrA9qPLq88b7eqBXSUcdL1utUtUfcSD": "OKX 22",  # added 2024-04-19
    "HnLezgQkNVWMp2AV6mcHM1Ljst32kfsoVnYUkvXuQpjg": "OKX 23",  # added 2024-04-19
    "44DwSkZbY2PVo9wfFP21D7HG7xf5rw8QaF6xSLGSfm7P": "OKX 30",  # added 2024-04-19
    "5URmUviWsAtcciFXqgM7f1jmhvBULdQDT47dqDkrJUi": "OKX 31",  # added 2024-04-19
    "5kiqH41rz2wPoNH5FuDJ3x8dB3EEe3XGoSWy2485tAp9": "OKX 32",  # added 2024-04-19
    "5VCwKtCXgCJ6kit5FybXjvriW3xELsFDhYrPSqtJNmcD": "OKX 33",  # added 2024-04-19
    "28nYGHJyUVcVdxZtzKByBXEj127XnrUkrE3VaGuWj1ZU": "Ceffu 1",  # added 2025-07-07
    "5SDrsMNTYdhmApjfqYHDvjoW92f2S42vcc7zNDVcQ9Ej": "Ceffu 2",  # added 2025-07-07
    "BZ3kabSsMzbuJUguYxtmkRtzw7ACqw1DUMH8PcbvXiUr": "Ceffu 3",  # added 2025-07-07
}
