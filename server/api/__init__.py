try:  # certificats de l'OS pour les appels HTTPS (Windows, proxy d'entreprise), comme l'ingestion
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass
