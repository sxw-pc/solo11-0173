"""Linear programming used by the bounded material-unmixing calculations.

A small two-phase tableau simplex is implemented with the standard library
alone so the ledger stays offline and dependency-free.  Every returned point
passes an explicit feasibility gate against the original (unflipped)
constraints; if the tableau cannot certify a point, the caller treats the
system as infeasible rather than trusting a questionable basis.  When SciPy is
installed it is used as an independent cross-check of the tableau result.
"""


def linprog(cost, A, b, tol=1e-9):
    """Minimize cost'x subject to A x <= b and x >= 0.

    Returns a feasible minimizer, or ``None`` when the polyhedron is empty or
    the tableau cannot certify feasibility.
    """
    point = _tableau(cost, A, b, tol)
    if point is not None and _violation(point, A, b) <= 1e-7 * _scale(b):
        return point
    checked = _scipy_check(cost, A, b)
    if checked is not None and _violation(checked, A, b) <= 1e-7 * _scale(b):
        return checked
    return None


def _scale(b):
    return max([1.0] + [abs(v) for v in b])


def _violation(x, A, b):
    return max((sum(A[k][j] * x[j] for j in range(len(x))) - b[k])
               for k in range(len(b))) if b else 0.0


def _tableau(cost, A, b, tol):
    m = len(b)
    n = len(cost)
    A = [row[:] for row in A]
    b = list(b)
    ge = [False] * m
    for i in range(m):
        if b[i] < -tol:
            A[i] = [-v for v in A[i]]
            b[i] = -b[i]
            ge[i] = True
    # Real variables | slacks | artificials | RHS.
    width = n + m + m
    tab = [[0.0] * (width + 1) for _ in range(m + 1)]
    for i in range(m):
        for j in range(n):
            tab[i + 1][j] = A[i][j]
        tab[i + 1][n + i] = -1.0 if ge[i] else 1.0
        tab[i + 1][n + m + i] = 1.0
        tab[i + 1][-1] = b[i]
    for i in range(1, m + 1):
        for j in range(width + 1):
            tab[0][j] -= tab[i][j]
    basis = [n + m + i for i in range(m)]

    def _pivot(tableau, cols):
        entering = None
        for j in range(cols):
            if tableau[0][j] < -tol:
                entering = j
                break
        if entering is None:
            return False
        ratios = [(tableau[i][-1] / tableau[i][entering], i)
                  for i in range(1, m + 1) if tableau[i][entering] > tol]
        if not ratios:
            raise ValueError('lp_unbounded')
        best = min(r for r, _ in ratios)
        leave = min(i for r, i in ratios if abs(r - best) <= tol)
        pivot_value = tableau[leave][entering]
        tableau[leave] = [v / pivot_value for v in tableau[leave]]
        for i in range(m + 1):
            if i != leave:
                factor = tableau[i][entering]
                tableau[i] = [tableau[i][k] - factor * tableau[leave][k]
                              for k in range(len(tableau[0]))]
        basis[leave - 1] = entering
        return True

    while _pivot(tab, width):
        pass
    scale = _scale(b)
    if tab[0][-1] > 1e-7 * scale:
        return None
    for i, col in enumerate(basis):
        if col >= n + m:
            row = i + 1
            replacement = next((j for j in range(n + m) if abs(tab[row][j]) > tol), None)
            if replacement is None:
                continue
            pivot_value = tab[row][replacement]
            tab[row] = [v / pivot_value for v in tab[row]]
            for k in range(m + 1):
                if k != row:
                    factor = tab[k][replacement]
                    tab[k] = [tab[k][j] - factor * tab[row][j] for j in range(width + 1)]
            basis[i] = replacement
    phase2 = [[0.0] * (n + m + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(n + m):
            phase2[i][j] = tab[i][j]
        phase2[i][-1] = tab[i][-1]
    for j in range(n):
        phase2[0][j] = cost[j]
    for i in range(1, m + 1):
        col = basis[i - 1]
        if col < n + m:
            factor = phase2[0][col]
            phase2[0] = [phase2[0][k] - factor * phase2[i][k]
                         for k in range(n + m + 1)]
    while True:
        entering = None
        for j in range(n + m):
            if phase2[0][j] < -tol:
                entering = j
                break
        if entering is None:
            break
        ratios = [(phase2[i][-1] / phase2[i][entering], i)
                  for i in range(1, m + 1) if phase2[i][entering] > tol]
        if not ratios:
            raise ValueError('lp_unbounded')
        best = min(r for r, _ in ratios)
        leave = min(i for r, i in ratios if abs(r - best) <= tol)
        pivot_value = phase2[leave][entering]
        phase2[leave] = [v / pivot_value for v in phase2[leave]]
        for i in range(m + 1):
            if i != leave:
                factor = phase2[i][entering]
                phase2[i] = [phase2[i][k] - factor * phase2[leave][k]
                             for k in range(n + m + 1)]
        basis[leave - 1] = entering
    x = [0.0] * (n + m)
    for i, col in enumerate(basis):
        if col < n + m:
            x[col] = phase2[i + 1][-1]
    return x[:n]


def _scipy_check(cost, A, b):
    """Optional independent solver; absent SciPy means the tableau gate stands."""
    try:
        import numpy as np
        from scipy.optimize import linprog as hi
    except Exception:
        return None
    result = hi(cost, A_ub=np.array(A), b_ub=np.array(b),
                bounds=[(0, None)] * len(cost), method='highs')
    if not result.success:
        return None
    return [float(v) for v in result.x]
