class Grid():
#clean up naming here, shits gross and unsatisfactory
    def __init__(self, midline: float, interval_percent: float, num_sections: int):
        self.interval = interval_percent
        self.lines=[round(midline * (1 + i * self.interval), 2) for i in range(-num_sections, num_sections + 1)]