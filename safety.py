"""Shared, inclusive client-coordinate input restrictions."""
import math
import config


def zones():
    result = [(config.FORBIDDEN_CLICK_X_MIN, config.FORBIDDEN_CLICK_X_MAX,
               config.FORBIDDEN_CLICK_Y_MIN, math.inf)]
    for name in vars(config):
        if name.startswith('FORBIDDEN_ZONE_') and name.endswith('_X_MIN'):
            prefix = name[:-5]
            result.append(tuple(getattr(config, prefix + suffix)
                                for suffix in ('X_MIN', 'X_MAX', 'Y_MIN', 'Y_MAX')))
    return result


def forbidden(x, y):
    return any(a <= x <= b and c <= y <= d for a, b, c, d in zones())


def allowed(point, size):
    x, y = point
    return (math.isfinite(x) and math.isfinite(y) and
            0 <= x < size[0] and 0 <= y < size[1] and not forbidden(x, y))


def path_allowed(start, end, size):
    if not allowed(start, size) or not allowed(end, size):
        return False
    # Slab intersection checks the continuous segment, including boundaries.
    for a, b, c, d in zones():
        lo, hi = 0., 1.
        for origin, delta, low, high in ((start[0], end[0]-start[0], a, b),
                                       (start[1], end[1]-start[1], c, d)):
            if delta == 0:
                if not low <= origin <= high:
                    lo, hi = 1., 0.
                    break
            else:
                u, v = (low-origin)/delta, (high-origin)/delta
                lo, hi = max(lo, min(u, v)), min(hi, max(u, v))
        if lo <= hi:
            return False
    return True
