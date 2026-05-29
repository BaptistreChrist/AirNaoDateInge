"""
Calcul de l'Indice de Qualité de l'Air (IQA) selon la formule EPA AQI.
Valeurs en µg/m³ (format retourné par l'API airpl.org).
Breakpoints SO2/NO2 convertis depuis ppb (×2.664 et ×1.912 respectivement).
Résultat : 0–500, catégories identiques à la maquette du projet.
"""

BREAKPOINTS = {
    "PM25": [
        (0.0,   12.0,   0,   50),
        (12.1,  35.4,  51,  100),
        (35.5,  55.4, 101,  150),
        (55.5, 150.4, 151,  200),
        (150.5, 250.4, 201, 300),
        (250.5, 500.4, 301, 500),
    ],
    "PM10": [
        (0,   54,   0,   50),
        (55,  154,  51,  100),
        (155, 254, 101,  150),
        (255, 354, 151,  200),
        (355, 424, 201,  300),
        (425, 604, 301,  500),
    ],
    "O3": [
        (0.0,   107.7,   0,   50),
        (107.8, 140.0,  51,  100),
        (140.1, 169.5, 101,  150),
        (169.6, 210.0, 151,  200),
        (210.1, 400.0, 201,  300),
    ],
    "NO2": [
        (0.0,   101.0,   0,   50),
        (101.1, 191.0,  51,  100),
        (191.1, 688.0, 101,  150),
        (688.1, 1241.0, 151, 200),
        (1241.1, 2389.0, 201, 300),
        (2389.1, 3921.0, 301, 500),
    ],
    "SO2": [
        (0.0,   93.2,   0,   50),
        (93.3,  199.8,  51,  100),
        (199.9, 492.8, 101,  150),
        (492.9, 810.2, 151,  200),
        (810.3, 1608.0, 201, 300),
        (1608.1, 2674.0, 301, 500),
    ],
}

CATEGORIES = [
    (0,   50,  "Bon"),
    (51,  100, "Modéré"),
    (101, 150, "Mauvais pour les groupes sensibles"),
    (151, 200, "Mauvais"),
    (201, 300, "Très mauvais"),
    (301, 500, "Dangereux"),
]


def _sub_index(notation: str, concentration: float) -> float | None:
    """Retourne le sous-indice AQI pour un polluant et une concentration donnés."""
    breakpoints = BREAKPOINTS.get(notation)
    if breakpoints is None or concentration is None:
        return None

    for c_low, c_high, i_low, i_high in breakpoints:
        if c_low <= concentration <= c_high:
            return ((i_high - i_low) / (c_high - c_low)) * (concentration - c_low) + i_low

    # Au-delà de la dernière borne : indice max
    if concentration > breakpoints[-1][1]:
        return 500.0
    return None


def compute_iqa(concentrations: dict[str, float | None]) -> dict:
    """
    concentrations : {"O3": 45.2, "PM10": 12.0, "PM25": 8.5, "NO2": 30.0, "SO2": 5.0}
    Retourne le sous-indice de chaque polluant et le global IQA (max des sous-indices).
    """
    sub_indices = {}
    for notation, value in concentrations.items():
        if value is not None:
            sub_indices[notation] = _sub_index(notation, value)

    valid = {k: v for k, v in sub_indices.items() if v is not None}
    iqa = max(valid.values()) if valid else None
    dominant = max(valid, key=valid.get) if valid else None

    category = None
    if iqa is not None:
        for low, high, label in CATEGORIES:
            if low <= iqa <= high:
                category = label
                break

    return {
        "iqa": round(iqa, 1) if iqa is not None else None,
        "categorie": category,
        "polluant_dominant": dominant,
        "sous_indices": {k: round(v, 1) for k, v in sub_indices.items() if v is not None},
    }
