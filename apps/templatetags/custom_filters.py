from django import template

register = template.Library()

@register.filter(name='add_class')
def add_class(value, arg):
    return value.as_widget(attrs={'class': arg})

@register.filter
def class_name(value):
    return value.__class__.__name__.replace('Core', '').replace('Server', ' Server')

@register.filter
def get_item(dictionary, key):
    """Get item from dictionary by key"""
    if isinstance(dictionary, dict):
        return dictionary.get(key, {})
    return {}
