"""Extract optional public interaction data from a ytscrape next response."""
import re
from youtube_ledger import walk_named, text_of, parse_count


def _find_like(value):
    if isinstance(value, dict):
        if value.get('iconName') == 'LIKE':
            label = value.get('accessibilityText', '')
            count = parse_count(label)
            return count if count is not None else parse_count(value.get('title'))
        for child in value.values():
            result = _find_like(child)
            if result is not None:
                return result
    elif isinstance(value, list):
        for child in value:
            result = _find_like(child)
            if result is not None:
                return result
    return None


def interaction_metrics(data):
    primary = next(walk_named(data, 'videoPrimaryInfoRenderer'), {})
    like = next(walk_named(primary, 'segmentedLikeDislikeButtonViewModel'), {})
    owner = next(walk_named(data, 'videoOwnerRenderer'), {})
    subscriber_text = text_of(owner.get('subscriberCountText'))
    subscribers = parse_count(subscriber_text)
    if re.search(r'\d(?:\.\d+)?\s*[KMB]\b', subscriber_text, re.I):
        subscribers = '约 ' + subscriber_text
    comments = None
    for panel in walk_named(data, 'engagementPanelTitleHeaderRenderer'):
        if 'comment' in text_of(panel.get('title')).lower():
            comments = parse_count(panel.get('contextualInfo'))
            break
    return {'likes': _find_like(like), 'comments': comments, 'subscribers': subscribers}
