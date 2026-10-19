import math

# =====================================================================
# Camera: orthographic 3D -> 2D projection for the plant view. No Tk in here, so it can be tested.
# World axes in metres: X right, Y towards the viewer on the ground plane, Z up (elevation).
# yaw turns the plant around the vertical axis; pitch is the viewing angle (90 degrees = straight down,
# small = from the side). `project` gives screen pixels, `to_ground` goes back from a screen point to the
# ground plane at a given elevation (used to drag equipment around).
# =====================================================================

MIN_PITCH = math.radians(12)
MAX_PITCH = math.radians(90)


class Camera:
    def __init__(self, yaw_deg=30.0, pitch_deg=55.0, scale=12.0, origin=(450.0, 300.0), centre=(0.0, 0.0, 0.0)):
        self.yaw = math.radians(yaw_deg)
        self.pitch = math.radians(pitch_deg)
        self.scale = scale                  # pixels per metre
        self.origin = origin                # screen position of `centre`
        self.centre = centre                # world point that stays fixed on screen (metres)

    def rotate(self, d_yaw, d_pitch):
        self.yaw += d_yaw
        self.pitch = max(MIN_PITCH, min(MAX_PITCH, self.pitch + d_pitch))

    def zoom(self, factor):
        self.scale = max(1.0, min(200.0, self.scale * factor))

    def _view(self, x, y, z):
        x, y, z = x - self.centre[0], y - self.centre[1], z - self.centre[2]
        x1 = x * math.cos(self.yaw) - y * math.sin(self.yaw)
        y1 = x * math.sin(self.yaw) + y * math.cos(self.yaw)
        return x1, y1, z

    def project(self, x, y, z=0.0):
        """Screen (sx, sy) and nearness (bigger = closer to the viewer, for painter's sorting)."""
        x1, y1, z = self._view(x, y, z)
        sx = self.origin[0] + self.scale * x1
        sy = self.origin[1] + self.scale * (y1 * math.sin(self.pitch) - z * math.cos(self.pitch))
        return sx, sy, z * math.sin(self.pitch) - y1 * math.cos(self.pitch)

    def to_ground(self, sx, sy, z=0.0):
        """World (x, y) of the point on the horizontal plane at elevation z that is drawn at (sx, sy)."""
        x1 = (sx - self.origin[0]) / self.scale
        dz = z - self.centre[2]
        y1 = ((sy - self.origin[1]) / self.scale + dz * math.cos(self.pitch)) / math.sin(self.pitch)
        x = x1 * math.cos(self.yaw) + y1 * math.sin(self.yaw) + self.centre[0]
        y = -x1 * math.sin(self.yaw) + y1 * math.cos(self.yaw) + self.centre[1]
        return x, y
