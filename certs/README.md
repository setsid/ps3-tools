# Sony's console CA

`a0.ww.np.dl.playstation.net` presents a certificate issued by Sony's own
private CA:

```
Subject: CN=*.ww.np.dl.playstation.net, O=Sony Interactive Entertainment Inc.
Issuer:  CN=SCEI DNAS Root 05, O=Sony Computer Entertainment Inc., C=JP
```

That root is not in any public trust store and never will be. The endpoint is
meant to be consumed by PlayStation hardware, which ships the root built in.
Windows, Python and the OS trust stores all correctly refuse it, on every
machine — this is not interception and it is not anybody's antivirus.

So the manifest connection is verified against **this root specifically**,
rather than against the system store. That is stricter than the public CA path,
not weaker: exactly one issuer is accepted, so genuine interception still
fails, and the sha1 that the package download depends on keeps its chain of
trust.

## Supplying it

`scei-dnas-root-05.pem` is not in this repository. To fetch it:

```
openssl s_client -showcerts -connect a0.ww.np.dl.playstation.net:443 \
    -servername a0.ww.np.dl.playstation.net </dev/null 2>/dev/null \
    | openssl x509 -outform PEM > certs/scei-dnas-root-05.pem
```

That captures the leaf. For the root, take the last certificate in the
`-showcerts` chain — the one whose subject is `CN=SCEI DNAS Root 05` — and save
that instead.

Check what you saved before trusting it:

```
openssl x509 -in certs/scei-dnas-root-05.pem -noout -subject -issuer -dates
```

Subject and issuer should both read `CN=SCEI DNAS Root 05, O=Sony Computer
Entertainment Inc., C=JP`: a root is self-signed.

Without this file the Game updates card will say it cannot verify the
connection and stop. It will not fall back to an unverified one.
